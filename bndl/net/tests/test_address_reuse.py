from bndl.net import watchdog
from bndl.net.tests.test_reconnect import ReconnectTestBase


class AddressReuseTest(ReconnectTestBase):
    node_count = 4

    def test_node_name_change(self):
        wdog_interval = watchdog.WATCHDOG_INTERVAL
        watchdog.WATCHDOG_INTERVAL = .5
        try:
            self.assertTrue(self.all_connected())

            node = self.nodes[0]
            node.name = 'something-else'
            for peer in node.peers.values():
                peer.disconnect_async(reason='unit-test', active=False).result()
                print('disconnected', peer.name)

            self.wait_connected()
            self.assertTrue(self.all_connected())
        finally:
            watchdog.WATCHDOG_INTERVAL = wdog_interval
