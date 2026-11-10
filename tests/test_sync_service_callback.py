import tempfile
import unittest
from pathlib import Path

from folder_sync import _run_sync_once


class SyncServiceCallbackTests(unittest.TestCase):
    def test_service_callback_runs_existing_sync_worker_logic(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory_a = root / "a"
            directory_b = root / "b"
            directory_a.mkdir()
            directory_b.mkdir()
            (directory_a / "sample.txt").write_text("from A", encoding="utf-8")
            messages = []

            result = _run_sync_once(
                str(directory_a), str(directory_b), messages.append
            )

            self.assertTrue(result.startswith("完成 —"))
            self.assertEqual(
                (directory_b / "sample.txt").read_text(encoding="utf-8"),
                "from A",
            )
            self.assertTrue(any("扫描目录 A" in item for item in messages))


if __name__ == "__main__":
    unittest.main()
