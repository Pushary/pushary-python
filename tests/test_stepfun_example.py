"""Exercise both the executable and the article's runnable code."""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "stepfun"


class StepFunExampleTests(unittest.TestCase):
    def check_example(self, folder: Path) -> None:
        result = subprocess.run([sys.executable, str(folder / "check_run.py")], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_persisted_order_approval(self) -> None:
        self.check_example(EXAMPLE)

    def test_article_runs_from_a_fresh_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            blocks = re.findall(r"```python\n(.*?)\n```", (EXAMPLE / "README.md").read_text(), re.S)
            self.assertTrue(blocks, "The reader needs the complete implementation")
            (folder / "run.py").write_text("\n\n".join(blocks) + "\n")
            shutil.copy(EXAMPLE / "check_run.py", folder)
            self.check_example(folder)


if __name__ == "__main__":
    unittest.main()
