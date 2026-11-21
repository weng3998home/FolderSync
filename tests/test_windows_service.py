import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import windows_service
import service_manager


class FakeWin32Service:
    SC_MANAGER_CONNECT = 1
    SERVICE_QUERY_CONFIG = 2
    SERVICE_QUERY_STATUS = 4
    SERVICE_STOP = 8
    SERVICE_STOPPED = 1
    SERVICE_START_PENDING = 2
    SERVICE_STOP_PENDING = 3
    SERVICE_RUNNING = 4
    SERVICE_CONTROL_STOP = 1

    def __init__(self, status):
        self.status = status
        self.calls = []
        self.open_service_args = None

    def OpenSCManager(self, *_):
        return "manager"

    def OpenService(self, *args):
        self.open_service_args = args
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


class FakeWin32Con:
    DELETE = 0x00010000


class FakeServiceCreator:
    SC_MANAGER_CONNECT = 1
    SC_MANAGER_CREATE_SERVICE = 2
    SC_MANAGER_ENUMERATE_SERVICE = 4
    SERVICE_ALL_ACCESS = 8
    SERVICE_WIN32_OWN_PROCESS = 16
    SERVICE_AUTO_START = 32
    SERVICE_ERROR_NORMAL = 64
    SERVICE_CONFIG_DESCRIPTION = 128

    def __init__(self):
        self.description_args = None

    def OpenSCManager(self, *_):
        return "manager"

    def CreateService(self, *_):
        return "service"

    def ChangeServiceConfig2(self, *args):
        self.description_args = args

    def StartService(self, *_):
        pass

    def CloseServiceHandle(self, _):
        pass


class FakeServiceStarter:
    SC_MANAGER_CONNECT = 1
    SERVICE_QUERY_CONFIG = 2
    SERVICE_QUERY_STATUS = 4
    SERVICE_CHANGE_CONFIG = 8
    SERVICE_START = 16
    SERVICE_NO_CHANGE = -1
    SERVICE_STOPPED = 1
    SERVICE_RUNNING = 4

    def __init__(self, status=SERVICE_STOPPED):
        self.status = status
        self.config_changes = []
        self.started = False

    def OpenSCManager(self, *_):
        return "manager"

    def OpenService(self, *_):
        return "service"

    def QueryServiceStatus(self, *_):
        return (0, self.status, 0, 0, 0, 0, 0)

    def ChangeServiceConfig(self, *args):
        self.config_changes.append(args)

    def StartService(self, *_):
        self.started = True
        self.status = self.SERVICE_RUNNING

    def CloseServiceHandle(self, _):
        pass


class WindowsServiceTests(unittest.TestCase):
    def test_platform_adapter_selects_windows_service_backend(self):
        self.assertTrue(service_manager.is_windows())
        self.assertTrue(service_manager.requires_account_password())
        self.assertIs(service_manager._backend(), windows_service)

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

    def test_service_config_validation_rejects_missing_fields_and_wrong_log_path(self):
        config = {
            "marker": windows_service.SERVICE_MARKER,
            "version": windows_service.CONFIG_VERSION,
            "service_name": "FolderData",
            "directory_a": "A",
            "directory_b": "B",
            "sync_path_a": "A",
            "sync_path_b": "B",
            "interval_minutes": 10,
            "account": "DOMAIN\\user",
            "binary_path": "folder_sync.exe --run-service FolderData",
            "log_path": str(windows_service.service_log_path("FolderData")),
        }
        self.assertEqual(
            windows_service._validate_service_config("FolderData", config),
            config,
        )
        legacy_config = dict(
            config,
            log_path=str(
                windows_service._program_data()
                / "logs"
                / "FolderData.log"
            ),
        )
        self.assertEqual(
            windows_service._validate_service_config(
                "FolderData", legacy_config
            ),
            legacy_config,
        )

        for key in ("sync_path_a", "account"):
            invalid_config = dict(config)
            invalid_config.pop(key)
            with self.subTest(missing=key):
                with self.assertRaisesRegex(
                    windows_service.ServiceManagerError, "配置无效"
                ):
                    windows_service._validate_service_config(
                        "FolderData", invalid_config
                    )

        invalid_config = dict(config, log_path="somewhere-else.log")
        with self.assertRaisesRegex(
            windows_service.ServiceManagerError, "日志路径"
        ):
            windows_service._validate_service_config(
                "FolderData", invalid_config
            )

    def test_service_data_acl_uses_minimum_access_and_is_not_duplicated(self):
        class FakeDacl:
            def __init__(self):
                self.aces = []
                self.add_count = 0

            def GetAceCount(self):
                return len(self.aces)

            def GetAce(self, index):
                return self.aces[index]

            def AddAccessAllowedAceEx(self, revision, flags, access_mask, sid):
                self.add_count += 1
                self.aces.append(
                    ((FakeWin32Con.ACCESS_ALLOWED_ACE_TYPE, flags, 0), access_mask, sid)
                )

        class FakeDescriptor:
            def __init__(self, dacl):
                self.dacl = dacl

            def GetSecurityDescriptorDacl(self):
                return self.dacl

            def SetSecurityDescriptorDacl(self, *_):
                pass

        class FakeSecurity:
            DACL_SECURITY_INFORMATION = 1
            ACL_REVISION_DS = 4
            dacl = FakeDacl()
            descriptor = FakeDescriptor(dacl)

            @staticmethod
            def LookupAccountName(*_):
                return "service-sid", None, None

            @staticmethod
            def GetFileSecurity(*_):
                return FakeSecurity.descriptor

            @staticmethod
            def SetFileSecurity(*_):
                pass

        class FakeWin32Con:
            ACCESS_ALLOWED_ACE_TYPE = 0
            CONTAINER_INHERIT_ACE = 1
            OBJECT_INHERIT_ACE = 2
            DELETE = 16

        fake_ntsecuritycon = type(
            "FakeNtSecurityCon",
            (),
            {"FILE_GENERIC_READ": 4, "FILE_GENERIC_WRITE": 8},
        )
        FakeSecurity.dacl = FakeDacl()
        FakeSecurity.descriptor = FakeDescriptor(FakeSecurity.dacl)
        path = Path("service-data")
        with patch.dict("sys.modules", {"ntsecuritycon": fake_ntsecuritycon}):
            windows_service._grant_account_access(
                path,
                "DOMAIN\\user",
                FakeWin32Con,
                FakeSecurity,
                fake_ntsecuritycon.FILE_GENERIC_READ
                | fake_ntsecuritycon.FILE_GENERIC_WRITE,
                FakeWin32Con.CONTAINER_INHERIT_ACE
                | FakeWin32Con.OBJECT_INHERIT_ACE,
            )
            windows_service._grant_account_access(
                path,
                "DOMAIN\\user",
                FakeWin32Con,
                FakeSecurity,
                fake_ntsecuritycon.FILE_GENERIC_READ
                | fake_ntsecuritycon.FILE_GENERIC_WRITE,
                FakeWin32Con.CONTAINER_INHERIT_ACE
                | FakeWin32Con.OBJECT_INHERIT_ACE,
            )

        self.assertEqual(FakeSecurity.dacl.add_count, 1)
        self.assertEqual(
            FakeSecurity.dacl.aces[0][1],
            fake_ntsecuritycon.FILE_GENERIC_READ
            | fake_ntsecuritycon.FILE_GENERIC_WRITE,
        )
        self.assertEqual(
            FakeSecurity.dacl.aces[0][0][1],
            FakeWin32Con.CONTAINER_INHERIT_ACE
            | FakeWin32Con.OBJECT_INHERIT_ACE,
        )

    def test_service_config_is_stored_in_account_accessible_directory(self):
        config = {
            "marker": windows_service.SERVICE_MARKER,
            "version": windows_service.CONFIG_VERSION,
            "service_name": "FolderData",
        }
        class FakeWin32Con:
            CONTAINER_INHERIT_ACE = 1
            OBJECT_INHERIT_ACE = 2

        fake_ntsecuritycon = type(
            "FakeNtSecurityCon",
            (),
            {"FILE_GENERIC_READ": 4, "FILE_GENERIC_WRITE": 8},
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                patch.object(
                    windows_service,
                    "_program_data",
                    return_value=Path(temporary_directory),
                ),
                patch.object(windows_service, "_grant_account_access") as grant,
                patch.dict("sys.modules", {"ntsecuritycon": fake_ntsecuritycon}),
            ):
                windows_service._write_config(
                    config, "DOMAIN\\user", FakeWin32Con, None
                )

                config_path = windows_service._config_path("FolderData")
                self.assertEqual(
                    config_path,
                    Path(temporary_directory)
                    / "services"
                    / "FolderData"
                    / "config.json",
                )
                self.assertTrue(config_path.is_file())
                self.assertTrue(
                    windows_service.service_log_path("FolderData").is_file()
                )
                self.assertEqual(
                    windows_service._load_config("FolderData"), config
                )
                self.assertEqual(
                    [call.args[0] for call in grant.call_args_list],
                    [
                        config_path.parent,
                        windows_service.service_log_path("FolderData"),
                    ],
                )
                self.assertEqual(grant.call_args_list[0].args[4], 4)
                self.assertEqual(grant.call_args_list[0].args[5], 3)
                self.assertEqual(grant.call_args_list[1].args[4], 12)
                self.assertEqual(grant.call_args_list[1].args[5], 0)

    def test_service_config_loads_legacy_location(self):
        config = {
            "marker": windows_service.SERVICE_MARKER,
            "version": windows_service.CONFIG_VERSION,
            "service_name": "FolderData",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(
                windows_service,
                "_program_data",
                return_value=Path(temporary_directory),
            ):
                path = windows_service._legacy_config_path("FolderData")
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(config), encoding="utf-8")

                self.assertEqual(
                    windows_service._load_config("FolderData"), config
                )

    def test_removing_service_config_keeps_log_for_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(
                windows_service,
                "_program_data",
                return_value=Path(temporary_directory),
            ):
                config_path = windows_service._config_path("FolderData")
                config_path.parent.mkdir(parents=True)
                config_path.write_text("config", encoding="utf-8")
                temporary_config_path = config_path.with_suffix(".tmp")
                temporary_config_path.write_text("partial", encoding="utf-8")
                log_path = windows_service.service_log_path("FolderData")
                log_path.write_text("service log", encoding="utf-8")

                windows_service._remove_config("FolderData")

                self.assertFalse(config_path.exists())
                self.assertFalse(temporary_config_path.exists())
                self.assertEqual(log_path.read_text(encoding="utf-8"), "service log")

    def test_service_runs_sync_round_and_logs_progress(self):
        class FakeServiceFramework:
            def __init__(self, _):
                self.statuses = []

            def ReportServiceStatus(self, status):
                self.statuses.append(status)

        class FakeServiceUtil:
            ServiceFramework = FakeServiceFramework

            @staticmethod
            def PrepareToHostSingle(service_class):
                FakeServiceUtil.service_class = service_class

            @staticmethod
            def StartServiceCtrlDispatcher():
                FakeServiceUtil.service_class([]).SvcRun()

        class FakeServiceManager:
            Initialize = staticmethod(lambda: None)
            LogErrorMsg = staticmethod(lambda _: None)
            PrepareToHostSingle = staticmethod(
                FakeServiceUtil.PrepareToHostSingle
            )
            StartServiceCtrlDispatcher = staticmethod(
                FakeServiceUtil.StartServiceCtrlDispatcher
            )

        class FakeWin32Event:
            WAIT_OBJECT_0 = 0
            WAIT_TIMEOUT = 258

            @staticmethod
            def CreateEvent(*_):
                return object()

            @staticmethod
            def SetEvent(_):
                pass

            @staticmethod
            def WaitForSingleObject(_, timeout):
                return (
                    FakeWin32Event.WAIT_TIMEOUT
                    if timeout == 0
                    else FakeWin32Event.WAIT_OBJECT_0
                )

        class FakeWin32Service:
            SERVICE_RUNNING = 4
            SERVICE_STOP_PENDING = 3

        config = {
            "marker": windows_service.SERVICE_MARKER,
            "version": windows_service.CONFIG_VERSION,
            "service_name": "FolderData",
            "directory_a": "A",
            "directory_b": "B",
            "interval_minutes": 1,
            "sync_path_a": "A",
            "sync_path_b": "B",
            "account": "DOMAIN\\user",
            "binary_path": "folder_sync.exe --run-service FolderData",
            "log_path": str(windows_service.service_log_path("FolderData")),
        }
        sync_calls = []
        log_lines = []

        def sync_once(directory_a, directory_b, log_callback):
            sync_calls.append((directory_a, directory_b))
            log_callback("同步内容")
            self.assertTrue(any("同步内容" in line for line in log_lines))
            return "完成 — 复制 1 个，删除 0 个"

        with (
            patch.object(
                windows_service,
                "_win32",
                return_value=(
                    FakeServiceManager,
                    None,
                    FakeWin32Event,
                    None,
                    FakeWin32Service,
                    FakeServiceUtil,
                ),
            ),
            patch.object(
                windows_service, "_load_config", return_value=config
            ),
            patch.object(
                windows_service,
                "_append_log",
                side_effect=lambda _, line: log_lines.append(line),
            ),
        ):
            self.assertEqual(
                windows_service.run_service("FolderData", sync_once), 0
            )

        self.assertEqual(sync_calls, [("A", "B")])
        self.assertTrue(any("第 1 轮同步开始" in line for line in log_lines))
        self.assertTrue(any("第 1 轮同步成功" in line for line in log_lines))

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

    def test_start_updates_scm_password_and_starts_owned_service(self):
        api = FakeServiceStarter()
        binary_path = r'"C:\FolderSync.exe" --run-service FolderData'
        config = {
            "marker": windows_service.SERVICE_MARKER,
            "version": windows_service.CONFIG_VERSION,
            "service_name": "FolderData",
            "binary_path": binary_path,
        }
        service_config = (
            0, 0, 0, binary_path, None, 0, None, "DOMAIN\\user", "FolderData"
        )
        with (
            patch.object(
                windows_service,
                "_win32",
                return_value=(None, None, None, None, api, None),
            ),
            patch.object(windows_service, "require_admin"),
            patch.object(windows_service, "_load_config", return_value=config),
            patch.object(
                windows_service,
                "_service_description",
                return_value=windows_service.SERVICE_MARKER,
            ),
            patch.object(
                windows_service,
                "_service_config",
                return_value=service_config,
            ),
        ):
            result = windows_service.start_managed_service(
                "FolderData", "new-password"
            )

        self.assertTrue(result)
        self.assertTrue(api.started)
        self.assertEqual(len(api.config_changes), 1)
        self.assertEqual(
            api.config_changes[0][8:10],
            ("DOMAIN\\user", "new-password"),
        )

    def test_start_does_not_change_credentials_when_service_is_running(self):
        api = FakeServiceStarter(FakeServiceStarter.SERVICE_RUNNING)
        binary_path = r'"C:\FolderSync.exe" --run-service FolderData'
        config = {
            "marker": windows_service.SERVICE_MARKER,
            "version": windows_service.CONFIG_VERSION,
            "service_name": "FolderData",
            "binary_path": binary_path,
        }
        service_config = (
            0, 0, 0, binary_path, None, 0, None, "DOMAIN\\user", "FolderData"
        )
        with (
            patch.object(
                windows_service,
                "_win32",
                return_value=(None, None, None, None, api, None),
            ),
            patch.object(windows_service, "require_admin"),
            patch.object(windows_service, "_load_config", return_value=config),
            patch.object(
                windows_service,
                "_service_description",
                return_value=windows_service.SERVICE_MARKER,
            ),
            patch.object(
                windows_service,
                "_service_config",
                return_value=service_config,
            ),
        ):
            result = windows_service.start_managed_service(
                "FolderData", "unused-password"
            )

        self.assertFalse(result)
        self.assertEqual(api.config_changes, [])
        self.assertFalse(api.started)

    def test_create_service_passes_description_as_unicode_string(self):
        api = FakeServiceCreator()
        with (
            patch.object(
                windows_service,
                "_win32",
                return_value=(None, None, None, None, api, None),
            ),
            patch.object(windows_service, "require_admin"),
            patch.object(windows_service, "_all_service_names", return_value=set()),
            patch.object(windows_service, "_binary_command", return_value="command"),
            patch.object(windows_service, "_write_config"),
            patch.object(windows_service, "_wait_until_running"),
        ):
            windows_service.create_service(
                r"C:\Data",
                r"C:\Backup",
                r"C:\Data",
                r"C:\Backup",
                15,
                "DOMAIN\\user",
                "password",
                "FolderData",
            )

        self.assertEqual(
            api.description_args,
            ("service", api.SERVICE_CONFIG_DESCRIPTION, windows_service.SERVICE_MARKER),
        )

    def test_delete_stops_running_owned_service_before_deleting(self):
        api = FakeWin32Service(FakeWin32Service.SERVICE_RUNNING)
        config = {
            "marker": windows_service.SERVICE_MARKER,
            "version": windows_service.CONFIG_VERSION,
            "service_name": "FolderPython",
            "binary_path": r'"C:\FolderSync.exe" --run-service FolderPython',
        }
        with (
            patch.object(windows_service, "_win32", return_value=(None, FakeWin32Con, None, None, api, None)),
            patch.object(windows_service, "is_admin", return_value=True),
            patch.object(windows_service, "_load_config", return_value=config),
            patch.object(windows_service, "_service_description", return_value=windows_service.SERVICE_MARKER),
            patch.object(windows_service, "_service_binary_path", return_value=config["binary_path"]),
            patch.object(windows_service, "_remove_config") as remove_config,
        ):
            windows_service.delete_service("FolderPython")
        remove_config.assert_called_once_with("FolderPython")
        self.assertEqual(api.calls, ["stop", "delete"])
        self.assertEqual(
            api.open_service_args[2],
            FakeWin32Service.SERVICE_QUERY_CONFIG
            | FakeWin32Service.SERVICE_QUERY_STATUS
            | FakeWin32Service.SERVICE_STOP
            | FakeWin32Con.DELETE,
        )

    def test_delete_refuses_unverified_service(self):
        api = FakeWin32Service(FakeWin32Service.SERVICE_RUNNING)
        with (
            patch.object(windows_service, "_win32", return_value=(None, FakeWin32Con, None, None, api, None)),
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
