"""trigger_check.sh: one run per new request, however the file was written."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TriggerCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        shutil.copy(ROOT / "trigger_check.sh", self.dir)
        (self.dir / "run_weekly.sh").write_text('echo run >> runs.txt\n')
        (self.dir / "trigger").mkdir()
        self.req = self.dir / "trigger" / "run-request"

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, *args):
        subprocess.run(["/bin/bash", str(self.dir / "trigger_check.sh"), *args], check=True)

    def runs(self):
        f = self.dir / "runs.txt"
        return len(f.read_text().splitlines()) if f.exists() else 0

    def test_no_request(self):
        self.check()
        self.assertEqual(self.runs(), 0)

    def test_marked_request_does_not_run(self):
        self.req.write_text("old\n")
        self.check("--mark")
        self.check()
        self.assertEqual(self.runs(), 0)

    def test_in_place_write_runs_once(self):
        self.req.write_text("old\n")
        self.check("--mark")
        self.req.write_text("new\n")
        self.check()
        self.check()
        self.assertEqual(self.runs(), 1)

    def test_atomic_replace_runs_once(self):
        self.req.write_text("old\n")
        self.check("--mark")
        tmp = self.dir / "trigger" / "tmp"
        tmp.write_text("new\n")
        os.replace(tmp, self.req)
        self.check()
        self.check()
        self.assertEqual(self.runs(), 1)

    def test_same_text_with_a_new_time_runs(self):
        self.req.write_text("same\n")
        self.check("--mark")
        st = self.req.stat()
        os.utime(self.req, (st.st_atime + 60, st.st_mtime + 60))
        self.check()
        self.assertEqual(self.runs(), 1)


if __name__ == "__main__":
    unittest.main()
