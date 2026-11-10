import unittest
from unittest.mock import patch

import windows_service


class FakeWin32Service:
    SC_MANAGER_CONNECT = 1
    SERVICE_QUERY_CONFIG = 2
    SERVICE_QUERY_STATUS = 4
    SERVICE_STOP = 8
    DELETE = 16
    SERVICE_STOPPED = 1
    SERVICE_START_PENDING = 2
    SERVICE_STOP_PENDING = 3
    SERVICE_RUNNING = 4
    SERVICE_CONTROL_STOP = 1

    def __init__(self, status):
        self.status = status
        self.calls = []

    def OpenSCManager(self, *_):
        return "manager"

    def OpenService(self, *_):
        return "service"

    def QueryServiceStatus(self, *_):
        return (0, self.status, 0, 0, 0, 0, 0)

    def ControlService(self, *_):
        self.calls.append("stop")
        self.status = self.SERVICE_STOPPED

    def DeleteService(self, *_):
        self.calls.append("delete")

    def CloseServiceHandle(self, _):
        pass


class WindowsServiceTests(unittest.TestCase):
    def test_service_name_is_sanitized_and_uses_last_directory(self):
        self.assertEqual(
            windows_service.make_service_name(r"E:\Data\Bad:Name"),
            "FolderBad_Name",
        )

    def test_name_conflicts_are_case_insensitive_and_numbered(self):
        self.assertEqual(
            windows_service.make_service_name(
                r"E:\Data\Python", ["folderpython", "FolderPython2"]
            ),
            "FolderPython3",
        )

    def test_name_handles_empty_leaf(self):
        self.assertEqual(windows_service.make_service_name(""), "FolderSync")

    def test_service_name_handles_comma_and_long_directory_names(self):
        self.assertEqual(
            windows_service.make_service_name(r"E:\Data\A,B"),
            "FolderA_B",
        )
        self.assertEqual(
            len(windows_service.make_service_name(r"E:\Data\\" + "X" * 300)),
            256,
        )

    def test_interval_requires_positive_integer(self):
        self.assertEqual(windows_service.validate_interval("15"), 15)
        for invalid in ("0", "-1", "1.5", "one", "", True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    windows_service.validate_interval(invalid)

    def test_admin_check_requires_elevation(self):
        with patch.object(windows_service, "is_admin", return_value=False):
            with self.assertRaisesRegex(
                windows_service.ServiceManagerError, "管理员权限"
            ):
                windows_service.require_admin()

    def test_service_logon_right_error_has_actionable_guidance(self):
        error = type("WindowsError", (Exception,), {"winerror": 1385})("failure")
        _, message = windows_service._windows_error(error)
        self.assertIn("作为服务登录", message)

    def test_service_ownership_requires_matching_marker_metadata_and_command(self):
        config = {
            "marker": windows_service.SERVICE_MARKER,
            "version": windows_service.CONFIG_VERSION,
            "service_name": "FolderPython",
            "binary_path": r'"C:\FolderSync.exe" --run-service FolderPython',
        }
        self.assertTrue(windows_service.is_managed_service(
            "FolderPython",
            windows_service.SERVICE_MARKER,
            config["binary_path"],
            config,
        ))
        self.assertFalse(windows_service.is_managed_service(
            "FolderOther",
            windows_service.SERVICE_MARKER,
            config["binary_path"],
            config,
        ))
        self.assertFalse(windows_service.is_managed_service(
            "FolderPython",
            "unrelated service",
            config["binary_path"],
            config,
        ))
        self.assertFalse(windows_service.is_managed_service(
            "FolderPython",
            windows_service.SERVICE_MARKER,
            r'"C:\Other.exe" --run-service FolderPython',
            config,
        ))

    def test_service_ownership_supports_quoted_names_with_spaces(self):
        service_name = "FolderMy Folder"
        binary_path = (
            r'"C:\FolderSync.exe" --run-service "FolderMy Folder"'
        )
        config = {
            "marker": windows_service.SERVICE_MARKER,
            "version": windows_service.CONFIG_VERSION,
            "service_name": service_name,
            "binary_path": binary_path,
        }
        self.assertTrue(windows_service.is_managed_service(
            service_name,
            windows_service.SERVICE_MARKER,
            binary_path,
            config,
        ))

    def test_status_text_reports_running_state(self):
        api = type("ServiceConstants", (), {
            "SERVICE_STOPPED": 1,
            "SERVICE_START_PENDING": 2,
            "SERVICE_STOP_PENDING": 3,
            "SERVICE_RUNNING": 4,
            "SERVICE_CONTINUE_PENDING": 5,
            "SERVICE_PAUSE_PENDING": 6,
            "SERVICE_PAUSED": 7,
        })()
        self.assertEqual(windows_service._status_text(4, api), "运行中")
        self.assertEqual(windows_service._status_text(87, api), "未知状态 (87)")

    def test_start_state_handler_rejects_stopped_service(self):
        api = type("ServiceConstants", (), {
            "SERVICE_RUNNING": 4,
            "SERVICE_STOPPED": 1,
            "QueryServiceStatus": lambda self, service: service.status,
        })()
        service = type("Service", (), {
            "status": (0, 1, 0, 1069, 0, 0, 0),
        })()
        with self.assertRaisesRegex(
            windows_service.ServiceStartError, "已安装，但启动失败"
        ):
            windows_service._wait_until_running(service, "FolderPython", api)

    def test_start_state_handler_accepts_running_service(self):
        api = type("ServiceConstants", (), {
            "SERVICE_RUNNING": 4,
            "SERVICE_STOPPED": 1,
            "QueryServiceStatus": lambda self, service: service.status,
        })()
        service = type("Service", (), {
            "status": (0, 4, 0, 0, 0, 0, 0),
        })()
        windows_service._wait_until_running(service, "FolderPython", api)

    def test_delete_stops_running_owned_service_before_deleting(self):
        api = FakeWin32Service(FakeWin32Service.SERVICE_RUNNING)
        config = {
            "marker": windows_service.SERVICE_MARKER,
            "version": windows_service.CONFIG_VERSION,
            "service_name": "FolderPython",
            "binary_path": r'"C:\FolderSync.exe" --run-service FolderPython',
        }
        with (
            patch.object(windows_service, "_win32", return_value=(None, None, None, None, api, None)),
            patch.object(windows_service, "is_admin", return_value=True),
            patch.object(windows_service, "_load_config", return_value=config),
            patch.object(windows_service, "_service_description", return_value=windows_service.SERVICE_MARKER),
            patch.object(windows_service, "_service_binary_path", return_value=config["binary_path"]),
        ):
            windows_service.delete_service("FolderPython")
        self.assertEqual(api.calls, ["stop", "delete"])

    def test_delete_refuses_unverified_service(self):
        api = FakeWin32Service(FakeWin32Service.SERVICE_RUNNING)
        with (
            patch.object(windows_service, "_win32", return_value=(None, None, None, None, api, None)),
            patch.object(windows_service, "is_admin", return_value=True),
            patch.object(windows_service, "_load_config", return_value=None),
            patch.object(windows_service, "_service_description", return_value="not FolderSync"),
            patch.object(windows_service, "_service_binary_path", return_value="other.exe"),
        ):
            with self.assertRaisesRegex(
                windows_service.ServiceManagerError, "拒绝删除"
            ):
                windows_service.delete_service("FolderPython")
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
