"""Раннер недоверенных тестов в Job Object (07: A2-07, E2E-R30)."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import base  # noqa: F401 — путь к tools/
import job_runner as jr

PY = [sys.executable, '-I', '-c']


class JobRunnerTests(unittest.TestCase):
    def test_normal_run_and_output(self):
        r = jr.run(PY + ['print("ok")'])
        self.assertEqual((r.returncode, r.stdout.strip()), (0, 'ok'))

    def test_memory_limit_stops_allocation(self):
        r = jr.run(PY + ['b = bytearray(600 * 1024 * 1024); print("выделено")'], mem_mb=200)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('выделено', r.stdout)

    def test_process_limit_stops_fork_bomb(self):
        code = ('import subprocess, sys\nps = []\nfor i in range(12):\n'
                '    ps.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"]))\nprint("все запущены")')
        r = jr.run(PY + [code], procs=4, timeout=60)
        self.assertNotIn('все запущены', r.stdout)

    def test_timeout_kills_whole_tree(self):
        mark = Path(tempfile.mkdtemp()) / 'alive.txt'
        child = 'import time, pathlib; time.sleep(4); pathlib.Path(%r).write_text("жив")' % str(mark)
        code = 'import subprocess, sys, time\nsubprocess.Popen([sys.executable, "-c", %r])\ntime.sleep(60)' % child
        t0 = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            jr.run(PY + [code], timeout=2)
        self.assertLess(time.time() - t0, 20)
        time.sleep(5)
        self.assertFalse(mark.exists())                                            # внук убит вместе с заданием

    def test_network_goes_to_closed_proxy_and_no_secrets(self):
        env = dict(os.environ, AWS_SECRET='x')
        import sandbox_env
        r = jr.run(PY + ['import os; print(os.environ.get("HTTPS_PROXY"), os.environ.get("AWS_SECRET"), os.environ.get("PIP_NO_INDEX"))'],
                   env=sandbox_env.minimal(base=env))
        self.assertEqual(r.stdout.split(), [jr.OFFLINE_PROXY, 'None', '1'])


if __name__ == '__main__':
    unittest.main()
