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

from functools import partial
import os
import signal
import time

from bndl.compute.tasks import current_node, TaskCancelled
from bndl.compute.tests import ComputeTest
from bndl.net.rmi import InvocationException


class TaskFailureTest(ComputeTest):
    executor_count = 5

    def test_assert_raises(self):
        try:
            self.ctx.range(10).map(lambda i: exec("raise ValueError('test')")).collect()
        except InvocationException as exc:
            self.assertIsInstance(exc.__cause__, ValueError)
            self.assertEqual(exc.__cause__.args, ('test',))
        else:
            self.assertFalse('did not raise InvocationException')


    def test_retry(self):
        def failon(executors, i):
            if current_node().name in executors:
                raise Exception()
            else:
                return i

        dset = self.ctx.range(10, pcount=self.ctx.executor_count)

        # test it can pass
        self.assertEqual(dset.map(partial(failon, [])).count(), 10)

        # test that it fails if there is no retry

        with self.assertRaises(Exception):
            dset.map(failon, [w.name for w in self.ctx.executors[:1]]).count()

        # test that it succeeds with a retry
        try:
            self.ctx.conf['bndl.compute.attempts'] = 2
            self.assertEqual(dset.map(failon, [w.name for w in self.ctx.executors[:1]]).count(), 10)
        finally:
            self.ctx.conf['bndl.compute.attempts'] = 1


    def test_cancel(self):
        executed = self.ctx.accumulator(set())
        cancelled = self.ctx.accumulator(set())
        failed = self.ctx.accumulator(set())

        def task(idx):
            try:
                nonlocal executed, cancelled, failed
                executed.update('add', idx)
                if idx > 1:
                    for _ in range(10):
                        time.sleep(.1)
                else:
                    time.sleep(.1)
            except TaskCancelled:
                cancelled.update('add', idx)
                raise
            except Exception:
                failed.update('add', idx)
                raise

            else:
                raise ValueError(idx)

        n_executors = self.ctx.executor_count

        try:
            self.ctx.range(1, n_executors * 2 + 1, pcount=n_executors * 2).map(task).execute()
        except InvocationException as exc:
            self.assertIsInstance(exc.__cause__, ValueError)
            self.assertEqual(exc.__cause__.args, (1,))
        else:
            self.assertFalse('did not raise InvocationException')

        time.sleep(.5)

        self.assertEqual(len(executed.value & set(range(1, n_executors + 1))), n_executors)
        self.assertEqual(len(cancelled.value & set(range(2, n_executors + 1))), n_executors - 1)
        self.assertEqual(len(failed.value), 0)


    def test_cache_miss_after_shuffle(self):
        try:
            self.ctx.conf['bndl.compute.attempts'] = 2

            dset = self.ctx.range(10).shuffle().cache()


            self.assertEqual(dset.count(), 10)
            self.assertEqual(dset.count(), 10)

            os.kill(self.ctx.executors[0].pid, signal.SIGKILL)
            self.assertEqual(dset.count(), 10)

            os.kill(self.ctx.executors[0].pid, signal.SIGKILL)
            self.assertEqual(dset.shuffle().count(), 10)
        finally:
            self.ctx.conf['bndl.compute.attempts'] = 1
