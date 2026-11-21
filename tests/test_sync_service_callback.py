import errno
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from folder_sync import (
    SyncWorker,
    _build_service_command_text,
    _build_service_start_command,
    _is_ignored_sync_entry,
    _run_sync_once,
)
from windows_service import ManagedService


class SyncServiceCallbackTests(unittest.TestCase):
    def test_service_command_text_includes_entrypoint_and_diagnostics(self):
        service = ManagedService(
            name="FolderData",
            directory_a=r"E:\Data",
            directory_b=r"E:\Backup",
            interval_minutes=10,
            status="已停止",
            account="DOMAIN\\user",
            binary_path=(
                r'"E:\work_vs\PythonDemo\.venv\Scripts\python.exe" '
                r'"E:\work_vs\PythonDemo\folder_sync\folder_sync.py" '
                r"--run-service FolderData"
            ),
            log_path=r"C:\ProgramData\FolderSync\logs\FolderData.log",
        )

        text = _build_service_command_text(service)

        self.assertIn(service.binary_path, text)
        self.assertIn("sc.exe start 'FolderData'", text)
        self.assertIn("sc.exe qc 'FolderData'", text)
        self.assertIn("sc.exe queryex 'FolderData'", text)
        self.assertIn("Get-Content 'C:\\ProgramData\\FolderSync", text)
        self.assertEqual(
            _build_service_start_command(service.name),
            "sc.exe start 'FolderData'",
        )

    def test_service_start_command_escapes_powershell_quotes(self):
        self.assertEqual(
            _build_service_start_command("FolderO'Brien"),
            "sc.exe start 'FolderO''Brien'",
        )

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

    def test_sync_scanner_excludes_system_and_temporary_files(self):
        ignored_files = (
            "Thumbs.db",
            "ehthumbs.db",
            "Desktop.ini",
            "backup.tmp",
            "backup.TEMP",
            "~autosave.TmP",
            "shortcut.LNK",
            "pagefile.sys",
            "hiberfil.sys",
            "swapfile.sys",
        )
        for name in ignored_files:
            with self.subTest(name=name):
                self.assertTrue(
                    _is_ignored_sync_entry(name, is_directory=False)
                )

        for name in ("$Recycle.Bin", "$RECYCLE.BIN", "System Volume Information"):
            with self.subTest(directory=name):
                self.assertTrue(
                    _is_ignored_sync_entry(name, is_directory=True)
                )
                self.assertFalse(
                    _is_ignored_sync_entry(name, is_directory=False)
                )

    def test_ignored_entries_are_not_copied_or_used_as_delete_commands(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory_a = root / "a"
            directory_b = root / "b"
            directory_a.mkdir()
            directory_b.mkdir()

            for name in (
                "Thumbs.db",
                "ehthumbs.db",
                "Desktop.ini",
                "backup.tmp",
                "backup.temp",
                "~autosave.tmp",
                "shortcut.lnk",
                "pagefile.sys",
                "hiberfil.sys",
                "swapfile.sys",
            ):
                (directory_a / name).write_text("ignore", encoding="utf-8")

            ignored_directory = directory_a / "$RECYCLE.BIN"
            ignored_directory.mkdir()
            (ignored_directory / "only-in-a.txt").write_text(
                "ignore", encoding="utf-8"
            )
            (ignored_directory / "DELimportant.txt").write_text(
                "ignore", encoding="utf-8"
            )
            target_directory = directory_b / "$RECYCLE.BIN"
            target_directory.mkdir()
            (target_directory / "important.txt").write_text(
                "keep", encoding="utf-8"
            )

            (directory_a / "keep.txt").write_text("copy", encoding="utf-8")
            messages = []
            result = _run_sync_once(
                str(directory_a), str(directory_b), messages.append
            )

            self.assertTrue(result.startswith("完成 —"))
            self.assertEqual(
                (directory_b / "keep.txt").read_text(encoding="utf-8"),
                "copy",
            )
            self.assertEqual(
                (target_directory / "important.txt").read_text(
                    encoding="utf-8"
                ),
                "keep",
            )
            self.assertFalse((directory_b / "Thumbs.db").exists())
            self.assertFalse((directory_b / "shortcut.lnk").exists())
            self.assertTrue((ignored_directory / "DELimportant.txt").exists())

    def test_file_operation_permission_error_is_logged_and_other_files_continue(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory_a = root / "a"
            directory_b = root / "b"
            directory_a.mkdir()
            directory_b.mkdir()
            (directory_a / "blocked.txt").write_text("blocked", encoding="utf-8")
            (directory_a / "available.txt").write_text("copy", encoding="utf-8")
            worker = SyncWorker(str(directory_a), str(directory_b))
            messages = []
            results = []
            worker.log.connect(messages.append)
            worker.finished_signal.connect(results.append)
            copy_file = worker._copy_file

            def copy_with_permission_error(source, destination, relative_path):
                if relative_path == "blocked.txt":
                    raise PermissionError(
                        errno.EACCES, "permission denied", str(destination)
                    )
                copy_file(source, destination, relative_path)

            with patch.object(
                worker, "_copy_file", side_effect=copy_with_permission_error
            ):
                worker.run()

            self.assertTrue(results[0].startswith("部分完成 —"))
            self.assertTrue((directory_b / "available.txt").exists())
            self.assertFalse((directory_b / "blocked.txt").exists())
            self.assertTrue(any("permission denied" in item for item in messages))
            self.assertTrue(any("blocked.txt" in item for item in messages))

    def test_incomplete_subtree_scan_blocks_del_deletion(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory_a = root / "a"
            directory_b = root / "b"
            marker = directory_a / "DEL_target"
            target = directory_b / "target"
            marker.mkdir(parents=True)
            target.mkdir(parents=True)
            (target / "keep.txt").write_text("keep", encoding="utf-8")
            worker = SyncWorker(str(directory_a), str(directory_b))
            messages = []
            results = []
            worker.log.connect(messages.append)
            worker.finished_signal.connect(results.append)
            with patch.object(
                worker,
                "_scan",
                side_effect=(
                    ({}, {"DEL_target": marker}, False),
                    ({}, {"target": target}, True),
                ),
            ):
                worker.run()

            self.assertTrue((target / "keep.txt").exists())
            self.assertTrue(marker.exists())
            self.assertTrue(results[0].startswith("部分完成 —"))
            self.assertTrue(
                any("不执行任何 DEL 删除指令" in item for item in messages)
            )

    def test_scanner_records_permission_error_and_marks_tree_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            worker = SyncWorker(str(root), str(root))
            messages = []
            worker.log.connect(messages.append)
            error = PermissionError(
                errno.EACCES, "permission denied", str(root / "private")
            )

            def walk_with_error(path, topdown, onerror):
                onerror(error)
                yield str(path), [], []

            with patch("folder_sync.os.walk", side_effect=walk_with_error):
                _, _, complete = worker._scan(root)

            self.assertFalse(complete)
            self.assertTrue(any("permission denied" in item for item in messages))
            self.assertTrue(any("private" in item for item in messages))

    def test_nested_del_file_is_not_copied_back_from_stale_scan_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory_a = root / "a"
            directory_b = root / "b"
            marker_directory = directory_a / "sub"
            target_directory = directory_b / "sub"
            marker_directory.mkdir(parents=True)
            target_directory.mkdir(parents=True)
            (marker_directory / "DELold.txt").write_text(
                "delete", encoding="utf-8"
            )
            (target_directory / "old.txt").write_text("old", encoding="utf-8")
            messages = []

            result = _run_sync_once(
                str(directory_a), str(directory_b), messages.append
            )

            self.assertTrue(result.startswith("完成 —"))
            self.assertFalse((marker_directory / "DELold.txt").exists())
            self.assertFalse((target_directory / "old.txt").exists())
            self.assertFalse((target_directory / "DEL_old.txt").exists())

    def test_incomplete_scan_keeps_del_file_and_does_not_sync_its_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory_a = root / "a"
            directory_b = root / "b"
            directory_a.mkdir()
            directory_b.mkdir()
            marker = directory_a / "DELitem.txt"
            target = directory_b / "item.txt"
            marker.write_text("delete", encoding="utf-8")
            target.write_text("keep until full scan", encoding="utf-8")
            worker = SyncWorker(str(directory_a), str(directory_b))
            results = []
            worker.finished_signal.connect(results.append)
            with patch.object(
                worker,
                "_scan",
                side_effect=(
                    ({"DEL_item.txt": marker}, {}, False),
                    ({"item.txt": target}, {}, True),
                ),
            ):
                worker.run()

            self.assertTrue(marker.exists())
            self.assertEqual(
                target.read_text(encoding="utf-8"), "keep until full scan"
            )
            self.assertTrue(results[0].startswith("部分完成 —"))

    def test_failed_del_directory_does_not_sync_from_stale_target_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory_a = root / "a"
            directory_b = root / "b"
            marker = directory_a / "DELtarget"
            target = directory_b / "target"
            marker.mkdir(parents=True)
            target.mkdir(parents=True)
            (target / "keep.txt").write_text("keep", encoding="utf-8")
            worker = SyncWorker(str(directory_a), str(directory_b))
            results = []
            worker.finished_signal.connect(results.append)
            remove_tree = shutil.rmtree

            def deny_target_removal(path, *args, **kwargs):
                if Path(path) == target:
                    raise PermissionError(
                        errno.EACCES, "permission denied", str(path)
                    )
                return remove_tree(path, *args, **kwargs)

            with patch(
                "folder_sync.shutil.rmtree",
                side_effect=deny_target_removal,
            ):
                worker.run()

            self.assertTrue(marker.exists())
            self.assertTrue((target / "keep.txt").exists())
            self.assertFalse((directory_a / "target" / "keep.txt").exists())
            self.assertTrue(results[0].startswith("部分完成 —"))


if __name__ == "__main__":
    unittest.main()
