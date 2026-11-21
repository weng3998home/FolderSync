import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ubuntu_service
import service_manager


class UbuntuServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.config_home = self.root / "config folder%tmp"
        self.state_home = self.root / "state folder%tmp"
        self.directory_a = self.root / "a"
        self.directory_b = self.root / "b"
        self.directory_a.mkdir()
        self.directory_b.mkdir()
        self.environment = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_STATE_HOME": str(self.state_home),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temporary_directory.cleanup)

    @staticmethod
    def _completed(command, stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def _create_service(self):
        name = ubuntu_service.next_service_name(str(self.directory_a))
        with patch.object(ubuntu_service, "_ensure_linger"), patch.object(
            ubuntu_service, "_run",
            side_effect=lambda command, **_kwargs: self._completed(
                command,
                stdout="active\n"
                if "show" in command else "",
            ),
        ):
            ubuntu_service.create_service(
                str(self.directory_a),
                str(self.directory_b),
                str(self.directory_a),
                str(self.directory_b),
                5,
                ubuntu_service.current_account_name(),
                "",
                name,
            )
        return name

    def test_create_service_is_user_scoped_and_never_stores_password(self):
        name = self._create_service()
        config_path = ubuntu_service._config_path(name)
        unit_path = ubuntu_service._unit_path(name)
        config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(config["account"], ubuntu_service.current_account_name())
        self.assertNotIn("password", config)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config_path.parent.stat().st_mode), 0o700)
        unit = unit_path.read_text(encoding="utf-8")
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("--run-user-service", unit)
        self.assertIn(
            ubuntu_service._systemd_quote(str(config_path)), unit
        )

    def test_create_rejects_a_password_for_ubuntu_user_service(self):
        name = ubuntu_service.next_service_name(str(self.directory_a))
        with self.assertRaisesRegex(
            ubuntu_service.ServiceManagerError, "不接受或保存账户密码"
        ):
            ubuntu_service.create_service(
                str(self.directory_a),
                str(self.directory_b),
                str(self.directory_a),
                str(self.directory_b),
                5,
                ubuntu_service.current_account_name(),
                "must-not-be-used",
                name,
            )

    def test_ubuntu_service_flow_does_not_require_account_password(self):
        with patch.object(service_manager, "is_windows", return_value=False):
            self.assertFalse(service_manager.requires_account_password())

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "Ubuntu platform import check"
    )
    def test_importing_application_on_linux_does_not_load_windows_service(self):
        source_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import folder_sync, sys; "
                "assert 'windows_service' not in sys.modules",
            ],
            cwd=source_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_linger_uses_one_bounded_polkit_authorization(self):
        username = ubuntu_service.current_account_name()
        responses = iter(("no\n", "yes\n"))
        with patch.object(
            ubuntu_service,
            "_run",
            side_effect=lambda command, **_kwargs: self._completed(
                command, stdout=next(responses)
            ),
        ), patch.object(
            ubuntu_service.shutil, "which", return_value="/usr/bin/pkexec"
        ), patch.object(
            ubuntu_service.subprocess,
            "run",
            return_value=self._completed(["pkexec"], returncode=0),
        ) as run:
            ubuntu_service._ensure_linger(username)

        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/pkexec", "loginctl", "enable-linger", username],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_missing_polkit_helper_provides_explicit_manual_command(self):
        with patch.object(
            ubuntu_service, "_run",
            return_value=self._completed(["loginctl"], stdout="no\n"),
        ), patch.object(ubuntu_service.shutil, "which", return_value=None):
            with self.assertRaisesRegex(
                ubuntu_service.ServiceManagerError,
                "sudo loginctl enable-linger",
            ):
                ubuntu_service._ensure_linger("current-user")

    def test_delete_requires_our_marker_and_preserves_logs(self):
        name = self._create_service()
        log_path = ubuntu_service.service_log_path(name)
        log_path.write_text("history", encoding="utf-8")
        with patch.object(
            ubuntu_service, "_run",
            side_effect=lambda command, **_kwargs: self._completed(command),
        ):
            ubuntu_service.delete_service(name)

        self.assertFalse(ubuntu_service._config_path(name).exists())
        self.assertFalse(ubuntu_service._unit_path(name).exists())
        self.assertEqual(log_path.read_text(encoding="utf-8"), "history")

    def test_list_and_start_use_current_user_systemd_manager(self):
        name = self._create_service()
        commands = []

        def run(command, **_kwargs):
            commands.append(command)
            return self._completed(
                command,
                stdout="inactive\n" if "show" in command else "",
            )

        with patch.object(ubuntu_service, "_run", side_effect=run):
            services = ubuntu_service.list_managed_services()
            started = ubuntu_service.start_managed_service(name)

        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].status, "已停止")
        self.assertEqual(services[0].account, ubuntu_service.current_account_name())
        self.assertTrue(started)
        self.assertTrue(
            any(command[1:3] == ["--user", "start"] for command in commands)
        )

    def test_delete_refuses_unit_without_folder_sync_marker(self):
        name = self._create_service()
        ubuntu_service._unit_path(name).write_text(
            "[Service]\nExecStart=/unrelated\n", encoding="utf-8"
        )
        with patch.object(ubuntu_service, "_run") as run:
            with self.assertRaisesRegex(
                ubuntu_service.ServiceManagerError,
                "无法验证为 FolderSync 创建",
            ):
                ubuntu_service.delete_service(name)
        run.assert_not_called()

    def test_failed_round_is_logged_and_the_next_round_retries(self):
        name = self._create_service()
        config_path = ubuntu_service._config_path(name)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["interval_minutes"] = 1
        config_path.write_text(json.dumps(config), encoding="utf-8")
        os.chmod(config_path, 0o600)
        clock = [0.0]
        handlers = {}
        attempts = []

        def sync_once(_directory_a, _directory_b, _log):
            attempts.append(True)
            if len(attempts) == 1:
                return "部分完成 — 目录不可访问"
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return "完成 — 复制 1 个，删除 0 个"

        def advance_clock(seconds):
            clock[0] += seconds

        with patch.object(
            ubuntu_service.signal,
            "signal",
            side_effect=lambda number, handler: handlers.__setitem__(number, handler),
        ), patch.object(
            ubuntu_service.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(
            ubuntu_service.time, "sleep", side_effect=advance_clock
        ):
            result = ubuntu_service.run_user_service(
                str(config_path), sync_once
            )

        log = ubuntu_service.service_log_path(name).read_text(encoding="utf-8")
        self.assertEqual(result, 0)
        self.assertEqual(len(attempts), 2)
        self.assertIn("第 1 轮同步失败", log)
        self.assertIn("第 2 轮同步成功", log)


if __name__ == "__main__":
    unittest.main()
