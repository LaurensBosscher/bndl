# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import logging
import os

from bndl.compute.blocks import BlockManager
from bndl.compute.broadcast import BroadcastManager
from bndl.compute.shuffle import ShuffleManager
from bndl.execute.worker import Worker as ExecutionWorker
from bndl.net import run
from bndl.net.connection import getlocalhostname
from bndl.run import supervisor
from bndl.util.exceptions import catch
import bndl


logger = logging.getLogger(__name__)


class Worker(ExecutionWorker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.services['blocks'] = BlockManager(self)
        self.services['broadcast'] = BroadcastManager(self)
        self.services['shuffle'] = ShuffleManager(self)


main_argparser = argparse.ArgumentParser(parents=[run.argparser])
many_argparser = argparse.ArgumentParser(parents=[run.argparser, supervisor.base_argparser], add_help=False)


def main():
    conf = bndl.conf
    args = main_argparser.parse_args()
    listen_addresses = args.listen_addresses or conf.get('bndl.net.listen_addresses')
    seeds = args.seeds or conf.get('bndl.net.seeds') or ['tcp://%s:5000' % getlocalhostname()]
    worker = Worker(addresses=listen_addresses, seeds=seeds)
    run.run_nodes(worker)


def run_workers():
    argparser = argparse.ArgumentParser(parents=[many_argparser])

    conf = bndl.conf
    def_worker_count = conf.get('bndl.compute.worker_count') or os.cpu_count() or 1
    argparser.add_argument('process_count', nargs='?', type=int, default=def_worker_count,
                            metavar='worker count', help='The number of workers to start (defaults'
                                                         ' to %s).' % def_worker_count)
    args = argparser.parse_args()
    args.entry_point = 'bndl.compute.worker', 'main'

    # reconstruct the arguments for the worker
    # parse_known_args doesn't take out the worker_count positional argument correctly
    worker_args = []
    if args.listen_addresses:
        worker_args += ['--listen-addresses'] + args.listen_addresses
    if args.seeds:
        worker_args += ['--seeds'] + args.seeds

    superv = supervisor.Supervisor.from_args(args, worker_args)
    superv.start()
    try:
        superv.wait()
    except KeyboardInterrupt:
        with catch():
            superv.stop()


if __name__ == '__main__':
    main()
