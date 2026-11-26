import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class ExecutionRecordTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.repo = Path(sandbox.name)
        self.pilot = self.repo / "pilots" / "research-usage"
        self.pilot.mkdir(parents=True)
        for name in ("summarize_usage.py", "workspace-usage-v1.csv", "expected-summary.json"):
            shutil.copyfile(Path(__file__).with_name(name), self.pilot / name)
        self.git("init", "-q")
        self.git("-c", "core.autocrlf=false", "add", "--all")
        self.git("-c", "user.name=Pilot Test", "-c", "user.email=pilot@example.test",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic pilot fixture")
        self.commit = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True, stderr=subprocess.PIPE)

    def receipt(self, commit=None):
        return subprocess.run(
            [sys.executable, str(self.pilot / "summarize_usage.py"), "--expected-commit", commit or self.commit],
            cwd=self.repo, capture_output=True, text=True,
        )

    def test_verifies_committed_inputs_and_produces_deterministic_record(self):
        first = self.receipt()
        self.assertEqual(first.returncode, 0, first.stderr)
        data = json.loads(first.stdout)
        self.assertEqual(data["source_commit"], self.commit)
        self.assertEqual(data["summary"]["totals"], {"vcpu_hours": "12.00", "memory_gib_hours": "25.00"})
        self.assertEqual(len(data["inputs"]["workspace-usage-v1.csv"]), 64)
        dataset = self.pilot / "workspace-usage-v1.csv"
        dataset.write_bytes(dataset.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(self.receipt().stdout, first.stdout)

    def test_rejects_wrong_commit_and_changed_input_even_if_summary_is_identical(self):
        wrong = self.receipt("0" * len(self.commit))
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("expected", wrong.stderr)
        dataset = self.pilot / "workspace-usage-v1.csv"
        dataset.write_bytes(dataset.read_bytes().replace(b"T08:00:00Z", b"T08:01:00Z"))
        changed = self.receipt()
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("workspace-usage-v1.csv differs", changed.stderr)


if __name__ == "__main__":
    unittest.main()
