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

from collections import OrderedDict
import itertools
import logging
import os
import signal
import threading
import time

from cytoolz.itertoolz import pluck

from bndl.compute.tests import ComputeTest
from bndl.util.collection import flatten


logger = logging.getLogger(__name__)


class ShuffleTest(ComputeTest):
    config = {
        'bndl.compute.memory.limit': 1
    }

    def _test_shuffle(self, size, sort, serialization, compression):
        def mapper(i):
            return str(i) * 10

        part0_data = set(map(mapper, range(0, size, 2)))
        part1_data = set(map(mapper, range(1, size, 2)))

        data = self.ctx.range(size, pcount=self.executor_count * 2).map(mapper)

        shuffled = data.shuffle(sort=sort, serialization=serialization, compression=compression) \
                       .shuffle(sort=sort, serialization=serialization, compression=compression,
                                pcount=3, partitioner=lambda i: int(i) % 2) \
                       .collect(parts=True)

        parts = list(filter(None, map(set, shuffled)))

        self.assertEqual(len(parts), 2)
        self.assertEqual(len(parts[0]), size / 2)
        self.assertEqual(len(parts[1]), size / 2)

        parts.sort(key=lambda part: min(part))
        self.assertEqual(parts[0], part0_data)
        self.assertEqual(parts[1], part1_data)


    @classmethod
    def _setup_tests(cls):
        sizes = [10, 100 * 1000]
        sorts = [True, False]
        serializations = ['marshal', 'pickle', 'json']
        compressions = [None, 'gzip', 'lz4']

        options = itertools.product(sizes, sorts, serializations, compressions)
        for size, sort, serialization, compression in options:
            opts = OrderedDict()
            opts['size'] = size
            opts['sort'] = sort
            opts['serialization'] = serialization
            opts['compression'] = compression

            setattr(
                cls,
                'test_shuffle_' + '_'.join(map(lambda o: str(o).lower(), opts.values())),
                lambda self, opts=opts: self._test_shuffle(**opts)
            )



class ShuffleCacheTest(ComputeTest):
    def test_shuffle_cache(self):
        def mapper(ctr, i):
            ctr += i
            return i
        ctr1 = self.ctx.accumulator(0)
        ctr2 = self.ctx.accumulator(0)

        dset = (self.ctx
            .range(10)
            .map(mapper, ctr1)
            .shuffle()
            .cache()
            .map(mapper, ctr2)
            .shuffle()
        )

        self.assertEqual(dset.count(), 10)
        self.assertEqual(ctr1.value, 45)
        self.assertEqual(ctr2.value, 45)

        self.assertEqual(dset.count(), 10)
        self.assertEqual(ctr1.value, 45)
        self.assertEqual(ctr2.value, 90)

        dset.cache()

        self.assertEqual(dset.count(), 10)
        self.assertEqual(ctr1.value, 45)
        self.assertEqual(ctr2.value, 135)

        self.assertEqual(dset.count(), 10)
        self.assertEqual(ctr1.value, 45)
        self.assertEqual(ctr2.value, 135)



class ExecutorKiller(object):
    def __init__(self, executor, count):
        self.count = count + 1
        self.executor = executor
        self.lock = threading.Lock()

    def countdown(self, i):
        with self.lock:
            self.count -= i
            if self.count == 0:
                logger.info('Killing %r', self.executor.name)
                os.kill(self.executor.pid, signal.SIGKILL)

    def __str__(self):
        return 'kill %s after %s elements' % (self.executor.name, self.count)


def identity_mapper(killers, key_count, i):
    for killer in killers:
        killer.update('countdown', 1)
    return i


def keyby_mapper(killers, key_count, i):
    identity_mapper(killers, key_count, i)
    return (i % key_count, i)


class ShuffleFailureTest(ComputeTest):
    executor_count = 10

    def _test_dependency_failure(self, dset_size, pcount, key_count, kill_after):
        try:
            self.ctx.conf['bndl.compute.attempts'] = 2

            killers = [
                self.ctx.accumulator(ExecutorKiller(executor, count))
                for executor, count in zip(self.ctx.executors, kill_after)
            ]

            dset = self.ctx \
                       .range(dset_size, pcount=pcount) \
                       .map(identity_mapper, killers, key_count) \
                       .shuffle() \
                       .map(keyby_mapper, killers, key_count) \
                       .aggregate_by_key(sum)

            result = dset.collect()
            self.assertEqual(len(result), key_count)
            self.assertEqual(sorted(pluck(0, result)), list(range(key_count)))
            self.assertEqual(sum(pluck(1, result)), sum(range(dset_size)))

            time.sleep(1)
        finally:
            self.ctx.conf['bndl.compute.attempts'] = 1


    @classmethod
    def _setup_tests(cls):
        # dset_size, pcount, key_count, killers
        test_cases = [
            [100, 30, 30, [75, 100, 125]],
            [100, 30, 30, [0, 50, 100]],
            [ 10, 3, 3, [10]],
            [ 10, 3, 3, [0]],
        ]

        for dset_size, pcount, key_count, kill_after in test_cases:
            args = (dset_size, pcount, key_count, kill_after)
            name = 'test_dependency_failure_' + '_'.join(map(str, flatten(args)))
            test = lambda self, args = args: self._test_dependency_failure(*args)
            setattr(cls, name, test)

ShuffleTest._setup_tests()
ShuffleFailureTest._setup_tests()
