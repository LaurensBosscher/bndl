import asyncio
import errno
import functools
import itertools
import logging
import os
import random
import socket

from bndl.net.connection import urlparse, Connection, filter_ip_addresses
from bndl.net.peer import PeerNode, PeerTable, HELLO_TIMEOUT
from bndl.util.aio import get_loop
from bndl.util.text import camel_to_snake


logger = logging.getLogger(__name__)



class Node(object):
    PeerNode = PeerNode

    _nodeids = itertools.count()

    def __init__(self, loop, name=None, addresses=None, seeds=None):
        self.loop = loop or get_loop()
        self.name = name or '.'.join(map(str, (next(Node._nodeids), os.getpid(), socket.getfqdn())))
        self.node_type = camel_to_snake(self.__class__.__name__)
        # create placeholders for the addresses this node will listen on
        self.servers = {
            address:None for address in
            (addresses or [
                'unix:///tmp/bndl-%s.socket' % self.name,
                'tcp://%s:%s' % (socket.getfqdn(), 5000),
            ])
        }
        # TODO ensure that if a seed can't be connected to, it is retried
        self.seeds = seeds or ()
        self.peers = PeerTable()
        self._peer_table_lock = asyncio.Lock()
        self._iotasks = []


    @property
    @functools.lru_cache()
    def addresses(self):
        return list(self.servers.keys())


    @property
    @functools.lru_cache()
    def ip_addresses(self):
        return set(filter_ip_addresses(self.addresses))


    def start_async(self):
        task = self.loop.create_task(self.start())
        self._iotasks.append(task)
        task.add_done_callback(self._iotasks.remove)


    @asyncio.coroutine
    def start(self):
        for address in list(self.servers.keys()):
            yield from self._start_server(address)
        # TODO ensure that if a seed can't be connected to, it is retried
        for seed in self.seeds:
            if seed not in self.servers:
                yield from self.PeerNode(self.loop, self, addresses=[seed]).connect()


    def stop_async(self):
        self.loop.create_task(self.stop())


    @asyncio.coroutine
    def stop(self):
        for peer in list(self.peers.values()):
            yield from peer.disconnect()
        self.peers.clear()

        for address, server in self.servers.items():
            if server:
                server.close()
                yield from server.wait_closed()
                address = urlparse(address)
                if address.scheme == 'unix' and os.path.exists(address.path):
                    os.remove(address.path)

        for task in self._iotasks[:]:
            task.cancel()


    @asyncio.coroutine
    def _start_server(self, address):
        parsed = urlparse(address)
        if parsed.scheme == 'tcp':
            yield from self._start_tcp_server(address, parsed)
        elif parsed.scheme == 'unix':
            yield from self._start_unix_server(address, parsed)
        else:
            raise ValueError('unsupported scheme %s in address %s' % (parsed.scheme, address))


    @asyncio.coroutine
    def _start_tcp_server(self, address, parsed):
        host, port = parsed.hostname, parsed.port or 5000

        server = None

        for port in range(port, port + 1000):
            try:
                server = yield from asyncio.start_server(self._serve, host, port)
                break
            except OSError as e:
                if e.errno == errno.EADDRINUSE:
                    continue
                else:
                    logger.exception('unable to open server socket')

        if not server:
            return

        if parsed.port != port:
            del self.servers[address]
            address = 'tcp://%s:%s' % (host, port)
        logger.info('server socket opened at %s', address)
        self.servers[address] = server


    @asyncio.coroutine
    def _start_unix_server(self, address, parsed):
        if os.path.exists(parsed.path):
            os.remove(parsed.path)
        server = yield from asyncio.start_unix_server(self._serve, parsed.path)
        self.servers[address] = server
        logger.info('server socket opened at %s (%s)', address, parsed.path)


    @asyncio.coroutine
    def _discovered(self, src, discovery):
        for name, addresses in discovery.peers:
            logger.debug('%s: %s discovered %s', self.name, src.name, name)
            with(yield from self._peer_table_lock):
                if name not in self.peers:
                    try:
                        peer = self.PeerNode(self.loop, self, addresses=addresses, name=name)
                        yield from peer.connect()
                    except:
                        logger.exception('unexpected error while connecting to discovered peer %s', name)


    @asyncio.coroutine
    def _serve(self, reader, writer):
        try:
            c = Connection(self.loop, reader, writer)
            yield from self.PeerNode(self.loop, self)._connected(c)
        except GeneratorExit:
            c.close()
        except:
            c.close()
            logger.exception('unable to accept connection from %s', c.peername())
            return


    @asyncio.coroutine
    def _peer_connected(self, peer):
        with(yield from self._peer_table_lock):
            known_peer = self.peers.get(peer.name)
            if known_peer:
                assert peer.is_connected
                if self.name == peer.name:
                    logger.debug('self connect attempt of %s', peer.name)
                    yield from peer.disconnect(reason='self connect')
                    return
                elif known_peer.is_connected and known_peer < peer:
                    logger.debug('already connected with %s, closing %s', peer.name, known_peer.conn)
                    yield from peer.disconnect(reason='already connected, old connection wins')
                    return
                else:
                    logger.debug('already connected with %s, closing %s', peer.name, known_peer.conn)
                    yield from known_peer.disconnect(reason='already connected, new connection wins')
                    del self.peers[peer.name]
            self.peers[peer.name] = peer

        task = self.loop.create_task(self._notifiy_peers(peer))
        self._iotasks.append(task)
        task.add_done_callback(self._iotasks.remove)

        return True


    @asyncio.coroutine
    def _notifiy_peers(self, new_peer):
        with(yield from self._peer_table_lock):
            peers = list(self.peers.filter())
            random.shuffle(peers)

        peer_list = list(
            (peer.name, peer.addresses)
            for peer in peers
            if peer.name != new_peer.name
        )

        try:
            if peer_list:
                yield from asyncio.wait_for(
                    new_peer._notify_discovery(peer_list),
                    timeout=HELLO_TIMEOUT * 3
                )
        except:
            logger.exception('discovery notification failed')

        for peer in peers:
            if peer.name != new_peer.name:
                yield from asyncio.sleep(.001)  # @UndefinedVariable
                try:
                    yield from asyncio.wait_for(
                        peer._notify_discovery([(new_peer.name, new_peer.addresses)]),
                        timeout=HELLO_TIMEOUT * 3
                    )
                except:
                    logger.exception('discovery notification failed')


    def __str__(self):
        return 'Node ' + self.name

