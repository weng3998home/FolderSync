"""Windows SCM integration for FolderSync."""

import ctypes
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable


SERVICE_MARKER = "FolderSync managed service; metadata v1"
CONFIG_VERSION = 1
SERVICE_PREFIX = "Folder"
_INVALID_SERVICE_CHARS = re.compile(r'[,<>:"/\\|?*\x00-\x1f]')
_MAX_WAIT_MS = 0xFFFFFFFE


class ServiceManagerError(RuntimeError):
    """A Windows service operation failed."""


class ServiceStartError(ServiceManagerError):
    """A service was installed but could not be started."""

    def __init__(self, service_name: str, message: str):
        super().__init__(message)
        self.service_name = service_name


@dataclass(frozen=True)
class ManagedService:
    name: str
    directory_a: str
    directory_b: str
    interval_minutes: int
    status: str
    account: str
    binary_path: str
    log_path: str


def validate_interval(value: str | int) -> int:
    if isinstance(value, bool):
        raise ValueError("同步间隔必须是正整数（分钟）。")
    text = str(value).strip()
    if not text.isdecimal() or int(text) <= 0:
        raise ValueError("同步间隔必须是正整数（分钟）。")
    return int(text)


def make_service_name(directory_a: str, existing_names=()) -> str:
    leaf = PureWindowsPath(directory_a.rstrip("\\/")).name
    safe_leaf = _INVALID_SERVICE_CHARS.sub("_", leaf).strip(" .")
    candidate = (SERVICE_PREFIX + (safe_leaf or "Sync"))[:256]
    names = {name.casefold() for name in existing_names}
    if candidate.casefold() not in names:
        return candidate

    number = 2
    while True:
        suffix = str(number)
        numbered_name = f"{candidate[:256 - len(suffix)]}{suffix}"
        if numbered_name.casefold() not in names:
            return numbered_name
        number += 1


def is_admin() -> bool:
    if os.name != "nt":
        return False
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def require_admin() -> None:
    if not is_admin():
        raise ServiceManagerError(
            "此操作需要管理员权限。请关闭程序后，右键选择“以管理员身份运行”，"
            "再刷新服务列表并重试。"
        )


def current_account_name() -> str:
    if os.name != "nt":
        raise ServiceManagerError("Windows 服务只能在 Windows 上使用。")
    try:
        import win32api
        import win32con
        return win32api.GetUserNameEx(win32con.NameSamCompatible)
    except ImportError as exc:
        raise ServiceManagerError(
            "缺少 Windows 服务依赖 pywin32，请重新安装应用或运行依赖安装。"
        ) from exc
    except Exception as exc:
        raise ServiceManagerError(f"无法获取当前 Windows 登录账户：{exc}") from exc


def _win32():
    if os.name != "nt":
        raise ServiceManagerError("Windows 服务只能在 Windows 上使用。")
    try:
        import servicemanager
        import win32con
        import win32event
        import win32security
        import win32service
        import win32serviceutil
    except ImportError as exc:
        raise ServiceManagerError(
            "缺少 Windows 服务依赖 pywin32，请重新安装应用或运行依赖安装。"
        ) from exc
    return (
        servicemanager,
        win32con,
        win32event,
        win32security,
        win32service,
        win32serviceutil,
    )


def _program_data() -> Path:
    root = os.environ.get("PROGRAMDATA")
    if not root:
        drive = os.environ.get("SystemDrive", "C:")
        root = str(Path(drive + "\\") / "ProgramData")
    return Path(root) / "FolderSync"


def service_log_path(service_name: str) -> Path:
    if not service_name or Path(service_name).name != service_name:
        raise ServiceManagerError("服务名称无效。")
    return _program_data() / "services" / service_name / "service.log"


def _config_path(service_name: str) -> Path:
    if not service_name or Path(service_name).name != service_name:
        raise ServiceManagerError("服务名称无效。")
    return _program_data() / "services" / service_name / "config.json"


def _legacy_config_path(service_name: str) -> Path:
    if not service_name or Path(service_name).name != service_name:
        raise ServiceManagerError("服务名称无效。")
    return _program_data() / "services" / f"{service_name}.json"


def _load_config(service_name: str) -> dict | None:
    path = _config_path(service_name)
    try:
        with path.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError:
        path = _legacy_config_path(service_name)
        try:
            with path.open("r", encoding="utf-8") as config_file:
                config = json.load(config_file)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ServiceManagerError(
                f"无法读取服务配置 {path}：{exc}"
            ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceManagerError(f"无法读取服务配置 {path}：{exc}") from exc

    if (
        not isinstance(config, dict)
        or config.get("marker") != SERVICE_MARKER
        or config.get("version") != CONFIG_VERSION
        or config.get("service_name") != service_name
    ):
        return None
    return config


def _validate_service_config(service_name: str, config: dict) -> dict:
    required_strings = (
        "directory_a",
        "directory_b",
        "sync_path_a",
        "sync_path_b",
        "account",
        "binary_path",
        "log_path",
    )
    if (
        not isinstance(config, dict)
        or config.get("marker") != SERVICE_MARKER
        or type(config.get("version")) is not int
        or config.get("version") != CONFIG_VERSION
        or config.get("service_name") != service_name
    ):
        raise ServiceManagerError("服务配置无效：标记、版本或服务名称不匹配。")

    for key in required_strings:
        value = config.get(key)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ServiceManagerError(f"服务配置无效：字段“{key}”缺失或格式错误。")

    try:
        interval = validate_interval(config.get("interval_minutes"))
    except ValueError as exc:
        raise ServiceManagerError(
            "服务配置无效：同步间隔必须是正整数（分钟）。"
        ) from exc
    if type(config.get("interval_minutes")) is not int:
        raise ServiceManagerError("服务配置无效：同步间隔必须是整数。")

    allowed_log_paths = (
        service_log_path(service_name),
        _program_data() / "logs" / f"{service_name}.log",
    )
    normalized_log_path = os.path.normcase(os.path.abspath(config["log_path"]))
    if normalized_log_path not in {
        os.path.normcase(os.path.abspath(path)) for path in allowed_log_paths
    }:
        raise ServiceManagerError(
            "服务配置无效：日志路径与此服务允许的日志位置不匹配。"
        )

    config["interval_minutes"] = interval
    return config


def is_managed_service(
    service_name: str,
    description: str,
    binary_path: str,
    config: dict | None,
) -> bool:
    config_binary_path = (
        config.get("binary_path") if isinstance(config, dict) else None
    )
    if (
        not isinstance(service_name, str)
        or not isinstance(description, str)
        or not isinstance(binary_path, str)
        or not isinstance(config, dict)
        or not isinstance(config_binary_path, str)
        or not service_name.startswith(SERVICE_PREFIX)
        or description != SERVICE_MARKER
        or config.get("marker") != SERVICE_MARKER
        or config.get("version") != CONFIG_VERSION
        or config.get("service_name") != service_name
        or not binary_path
        or os.path.normcase(binary_path) != os.path.normcase(config_binary_path)
    ):
        return False
    expected_argument = subprocess.list2cmdline(
        ["--run-service", service_name]
    ).casefold()
    return binary_path.casefold().endswith(expected_argument)


def _service_description(service, win32service) -> str:
    try:
        description = win32service.QueryServiceConfig2(
            service, win32service.SERVICE_CONFIG_DESCRIPTION
        )
    except Exception as exc:
        raise ServiceManagerError(f"无法读取服务说明：{exc}") from exc
    if isinstance(description, dict):
        return description.get("Description", "")
    return description or ""


def _service_binary_path(service, win32service) -> str:
    return _service_config(service, win32service)[3]


def _service_config(service, win32service):
    try:
        return win32service.QueryServiceConfig(service)
    except Exception as exc:
        raise ServiceManagerError(f"无法读取服务配置：{exc}") from exc


def _verified_service_config(
    service_name: str, service, win32service, action: str = "操作"
) -> dict:
    config = _load_config(service_name)
    description = _service_description(service, win32service)
    binary_path = _service_binary_path(service, win32service)
    if not is_managed_service(
        service_name, description, binary_path, config
    ):
        raise ServiceManagerError(
            f"拒绝{action}服务“{service_name}”：无法验证其由 FolderSync 创建和管理。"
        )
    return config


def _status_text(status: int, win32service) -> str:
    labels = {
        win32service.SERVICE_STOPPED: "已停止",
        win32service.SERVICE_START_PENDING: "正在启动",
        win32service.SERVICE_STOP_PENDING: "正在停止",
        win32service.SERVICE_RUNNING: "运行中",
        win32service.SERVICE_CONTINUE_PENDING: "正在继续",
        win32service.SERVICE_PAUSE_PENDING: "正在暂停",
        win32service.SERVICE_PAUSED: "已暂停",
    }
    return labels.get(status, f"未知状态 ({status})")


def _open_scm(win32service, access: int):
    try:
        return win32service.OpenSCManager(None, None, access)
    except Exception as exc:
        raise ServiceManagerError(
            f"无法连接 Windows 服务控制管理器：{exc}"
        ) from exc


def _all_service_names(manager, win32service) -> set[str]:
    names = set()
    for service_type in (
        win32service.SERVICE_WIN32,
        win32service.SERVICE_DRIVER,
    ):
        names.update(
            entry[0]
            for entry in win32service.EnumServicesStatus(
                manager, service_type, win32service.SERVICE_STATE_ALL
            )
        )
    return names


def list_managed_services() -> list[ManagedService]:
    _, _, _, _, win32service, _ = _win32()
    manager = _open_scm(
        win32service,
        win32service.SC_MANAGER_CONNECT | win32service.SC_MANAGER_ENUMERATE_SERVICE,
    )
    result = []
    try:
        try:
            services = win32service.EnumServicesStatus(
                manager,
                win32service.SERVICE_WIN32,
                win32service.SERVICE_STATE_ALL,
            )
        except Exception as exc:
            raise ServiceManagerError(f"无法读取 Windows 服务列表：{exc}") from exc

        for service_name, _, status in services:
            if not service_name.startswith(SERVICE_PREFIX):
                continue
            service = None
            try:
                service = win32service.OpenService(
                    manager,
                    service_name,
                    win32service.SERVICE_QUERY_CONFIG
                    | win32service.SERVICE_QUERY_STATUS,
                )
                config = _load_config(service_name)
                description = _service_description(service, win32service)
                service_config = _service_config(service, win32service)
                binary_path = service_config[3]
                if not is_managed_service(
                    service_name, description, binary_path, config
                ):
                    continue
                result.append(ManagedService(
                    service_name,
                    config["directory_a"],
                    config["directory_b"],
                    validate_interval(config["interval_minutes"]),
                    _status_text(status[1], win32service),
                    service_config[7],
                    binary_path,
                    config["log_path"],
                ))
            except ServiceManagerError:
                raise
            except Exception as exc:
                raise ServiceManagerError(
                    f"读取服务“{service_name}”失败：{exc}"
                ) from exc
            finally:
                if service:
                    win32service.CloseServiceHandle(service)
    finally:
        win32service.CloseServiceHandle(manager)
    return result


def next_service_name(directory_a: str) -> str:
    _, _, _, _, win32service, _ = _win32()
    manager = _open_scm(
        win32service,
        win32service.SC_MANAGER_CONNECT | win32service.SC_MANAGER_ENUMERATE_SERVICE,
    )
    try:
        try:
            service_names = _all_service_names(manager, win32service)
        except Exception as exc:
            raise ServiceManagerError(f"无法检查服务名称冲突：{exc}") from exc
        return make_service_name(directory_a, service_names)
    finally:
        win32service.CloseServiceHandle(manager)


def _grant_account_access(
    path: Path,
    account: str,
    win32con,
    win32security,
    access_mask: int,
    inheritance_flags: int,
):
    try:
        sid, _, _ = win32security.LookupAccountName(None, account)
        descriptor = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            raise ServiceManagerError(
                f"无法为服务账户 {account} 配置服务数据文件/目录权限："
                "对象使用空 DACL，无法安全地追加最小权限。"
            )
        for index in range(dacl.GetAceCount()):
            header, existing_mask, existing_sid = dacl.GetAce(index)
            if (
                header[0] == win32con.ACCESS_ALLOWED_ACE_TYPE
                and existing_sid == sid
                and existing_mask & access_mask == access_mask
                and header[1] & inheritance_flags == inheritance_flags
            ):
                return
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            inheritance_flags,
            access_mask,
            sid,
        )
        descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION, descriptor
        )
    except ServiceManagerError:
        raise
    except Exception as exc:
        raise ServiceManagerError(
            f"无法为服务账户 {account} 配置服务数据目录权限：{exc}"
        ) from exc


def _binary_command(service_name: str) -> str:
    if getattr(sys, "frozen", False):
        arguments = [sys.executable, "--run-service", service_name]
    else:
        entrypoint = Path(__file__).with_name("folder_sync.py").resolve()
        arguments = [sys.executable, str(entrypoint), "--run-service", service_name]
    return subprocess.list2cmdline(arguments)


def _windows_error(
    exc: Exception, password: str = ""
) -> tuple[int | None, str]:
    error_code = getattr(exc, "winerror", None)
    if error_code is None and getattr(exc, "args", None):
        if isinstance(exc.args[0], int):
            error_code = exc.args[0]
    if error_code in (1069, 1326, 1327):
        return error_code, (
            "服务账户登录失败。请确认密码和账户状态；如果密码正确，请管理员检查"
            "该账户是否具有“作为服务登录”权限。"
        )
    if error_code == 1385:
        return error_code, (
            "该账户没有“作为服务登录”权限。请由管理员在本机安全策略中"
            "授予此权限后重试；FolderSync 不会自动更换账户或降低安全性。"
        )
    message = str(exc)
    if password:
        message = message.replace(password, "[已隐藏]")
    return error_code, message


def _wait_until_running(service, service_name: str, win32service) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            status = win32service.QueryServiceStatus(service)
        except Exception as exc:
            raise ServiceStartError(
                service_name,
                f"服务“{service_name}”已安装，但无法确认启动状态：{exc}",
            ) from exc
        if status[1] == win32service.SERVICE_RUNNING:
            return
        if status[1] == win32service.SERVICE_STOPPED:
            exit_code = status[3]
            detail = f"Windows 服务状态为已停止（退出码 {exit_code}）。"
            if exit_code == 1069:
                detail += (
                    "这是 Windows 服务账户登录失败，程序代码尚未运行；"
                    "请用“sc.exe qc”确认登录账户，并更新该账户在服务中保存的密码。"
                    "也请确认账户未锁定/过期且具有“作为服务登录”权限。"
                    "此时服务日志可能尚未生成或没有新内容。"
                )
            raise ServiceStartError(
                service_name, f"服务“{service_name}”已安装，但启动失败：{detail}"
            )
        time.sleep(0.25)
    raise ServiceStartError(
        service_name, f"服务“{service_name}”已安装，但等待启动状态超时。"
    )


def start_managed_service(service_name: str, password: str) -> bool:
    """更新已安装服务的保存密码并启动；若服务已运行则不做修改。"""
    require_admin()
    _, _, _, _, win32service, _ = _win32()
    manager = _open_scm(win32service, win32service.SC_MANAGER_CONNECT)
    service = None
    try:
        access = (
            win32service.SERVICE_QUERY_CONFIG
            | win32service.SERVICE_QUERY_STATUS
            | win32service.SERVICE_CHANGE_CONFIG
            | win32service.SERVICE_START
        )
        service = win32service.OpenService(manager, service_name, access)
        _verified_service_config(service_name, service, win32service)

        status = win32service.QueryServiceStatus(service)
        if status[1] == win32service.SERVICE_RUNNING:
            return False
        if status[1] != win32service.SERVICE_STOPPED:
            _wait_until_running(service, service_name, win32service)
            return False

        account = _service_config(service, win32service)[7]
        if not account:
            raise ServiceManagerError(
                f"无法读取服务“{service_name}”配置的 Windows 登录账户。"
            )
        try:
            win32service.ChangeServiceConfig(
                service,
                win32service.SERVICE_NO_CHANGE,
                win32service.SERVICE_NO_CHANGE,
                win32service.SERVICE_NO_CHANGE,
                None,
                None,
                None,
                None,
                account,
                password,
                None,
            )
        except Exception as exc:
            code, detail = _windows_error(exc, password)
            raise ServiceManagerError(
                f"更新服务“{service_name}”账户 {account} 的登录密码失败"
                f"{f'（错误 {code}）' if code is not None else ''}：{detail}"
            ) from exc

        try:
            win32service.StartService(service, None)
        except Exception as exc:
            code, detail = _windows_error(exc, password)
            raise ServiceStartError(
                service_name,
                f"服务“{service_name}”已保存新密码，但启动请求失败"
                f"{f'（错误 {code}）' if code is not None else ''}：{detail}",
            ) from exc
        _wait_until_running(service, service_name, win32service)
        return True
    except ServiceManagerError:
        raise
    except Exception as exc:
        code, detail = _windows_error(exc, password)
        raise ServiceManagerError(
            f"启动服务“{service_name}”失败"
            f"{f'（错误 {code}）' if code is not None else ''}：{detail}"
        ) from exc
    finally:
        password = ""
        if service:
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)


def _write_config(config: dict, account: str, win32con, win32security) -> None:
    service_directory = _config_path(config["service_name"]).parent
    try:
        service_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ServiceManagerError(f"无法创建服务数据目录：{exc}") from exc
    try:
        import ntsecuritycon
    except ImportError as exc:
        raise ServiceManagerError(
            "缺少 Windows 服务依赖 pywin32，请重新安装应用或运行依赖安装。"
        ) from exc

    inheritable_flags = (
        win32con.CONTAINER_INHERIT_ACE | win32con.OBJECT_INHERIT_ACE
    )
    _grant_account_access(
        service_directory,
        account,
        win32con,
        win32security,
        ntsecuritycon.FILE_GENERIC_READ,
        inheritable_flags,
    )
    log_path = service_log_path(config["service_name"])
    try:
        log_path.touch(exist_ok=True)
    except OSError as exc:
        raise ServiceManagerError(f"无法创建服务日志文件：{exc}") from exc
    _grant_account_access(
        log_path,
        account,
        win32con,
        win32security,
        ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE,
        0,
    )

    path = _config_path(config["service_name"])
    temporary_path = path.with_suffix(".tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as config_file:
            json.dump(config, config_file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, path)
    except OSError as exc:
        message = f"无法保存服务配置：{exc}"
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            message += f"；临时配置清理失败：{cleanup_error}"
        raise ServiceManagerError(message) from exc


def _remove_config(service_name: str) -> None:
    errors = []
    config_path = _config_path(service_name)
    for path in (
        config_path,
        config_path.with_suffix(".tmp"),
        _legacy_config_path(service_name),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{path}：{exc}")
    if errors:
        raise ServiceManagerError(f"服务配置清理失败：{'；'.join(errors)}")


def create_service(
    directory_a: str,
    directory_b: str,
    sync_path_a: str,
    sync_path_b: str,
    interval_minutes: int,
    account: str,
    password: str,
    service_name: str,
) -> None:
    require_admin()
    interval = validate_interval(interval_minutes)
    _, win32con, _, win32security, win32service, _ = _win32()
    manager = _open_scm(
        win32service,
        win32service.SC_MANAGER_CONNECT
        | win32service.SC_MANAGER_CREATE_SERVICE
        | win32service.SC_MANAGER_ENUMERATE_SERVICE,
    )
    service = None
    try:
        existing_names = _all_service_names(manager, win32service)
        expected_name = make_service_name(directory_a, existing_names)
        if service_name != expected_name:
            raise ServiceManagerError(
                f"服务名已被占用或已不符合目录名称规则；请刷新后确认新服务名"
                f"“{expected_name}”。"
            )

        command = _binary_command(service_name)
        config = {
            "marker": SERVICE_MARKER,
            "version": CONFIG_VERSION,
            "service_name": service_name,
            "directory_a": directory_a,
            "directory_b": directory_b,
            "sync_path_a": sync_path_a,
            "sync_path_b": sync_path_b,
            "interval_minutes": interval,
            "account": account,
            "binary_path": command,
            "log_path": str(service_log_path(service_name)),
        }
        _write_config(config, account, win32con, win32security)
        try:
            service = win32service.CreateService(
                manager,
                service_name,
                service_name,
                win32service.SERVICE_ALL_ACCESS,
                win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL,
                command,
                None,
                0,
                None,
                account,
                password,
            )
            win32service.ChangeServiceConfig2(
                service,
                win32service.SERVICE_CONFIG_DESCRIPTION,
                SERVICE_MARKER,
            )
        except Exception as original_error:
            cleanup_errors = []
            if service:
                try:
                    win32service.DeleteService(service)
                except Exception as cleanup_error:
                    cleanup_errors.append(f"删除未完成的服务失败：{cleanup_error}")
            try:
                _remove_config(service_name)
            except ServiceManagerError as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
            if cleanup_errors:
                raise ServiceManagerError(
                    f"服务配置失败：{_windows_error(original_error, password)[1]}；"
                    f"清理也未完全成功：{'；'.join(cleanup_errors)}"
                ) from original_error
            raise

        try:
            win32service.StartService(service, None)
            _wait_until_running(service, service_name, win32service)
        except ServiceStartError:
            raise
        except Exception as exc:
            code, detail = _windows_error(exc, password)
            message = (
                f"服务“{service_name}”已安装并设为开机自动启动，但启动失败"
                f"{f'（错误 {code}）' if code is not None else ''}：{detail}"
            )
            raise ServiceStartError(service_name, message) from exc
    except ServiceManagerError:
        raise
    except Exception as exc:
        code, detail = _windows_error(exc, password)
        raise ServiceManagerError(
            f"安装服务“{service_name}”失败"
            f"{f'（错误 {code}）' if code is not None else ''}：{detail}"
        ) from exc
    finally:
        password = ""
        if service:
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)


def delete_service(service_name: str) -> None:
    require_admin()
    _, win32con, _, _, win32service, _ = _win32()
    manager = _open_scm(
        win32service,
        win32service.SC_MANAGER_CONNECT,
    )
    service = None
    try:
        service = win32service.OpenService(
            manager,
            service_name,
            win32service.SERVICE_QUERY_CONFIG
            | win32service.SERVICE_QUERY_STATUS
            | win32service.SERVICE_STOP
            | win32con.DELETE,
        )
        _verified_service_config(
            service_name, service, win32service, action="删除"
        )

        status = win32service.QueryServiceStatus(service)
        if status[1] != win32service.SERVICE_STOPPED:
            if status[1] != win32service.SERVICE_STOP_PENDING:
                try:
                    win32service.ControlService(
                        service, win32service.SERVICE_CONTROL_STOP
                    )
                except Exception as exc:
                    raise ServiceManagerError(
                        f"停止服务“{service_name}”失败：{exc}"
                    ) from exc
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status = win32service.QueryServiceStatus(service)
                if status[1] == win32service.SERVICE_STOPPED:
                    break
                time.sleep(0.25)
            else:
                raise ServiceManagerError(
                    f"服务“{service_name}”停止超时，未执行删除。"
                )

        try:
            win32service.DeleteService(service)
        except Exception as exc:
            raise ServiceManagerError(
                f"删除服务“{service_name}”失败：{exc}"
            ) from exc
        try:
            _remove_config(service_name)
        except ServiceManagerError as exc:
            raise ServiceManagerError(
                f"服务“{service_name}”已提交删除，但配置清理失败：{exc}"
                f"服务日志已保留在 {service_log_path(service_name)}。"
            ) from exc
    except ServiceManagerError:
        raise
    except Exception as exc:
        raise ServiceManagerError(
            f"无法删除服务“{service_name}”：{exc}"
        ) from exc
    finally:
        if service:
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)


def _append_log(log_path: Path, message: str) -> None:
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def run_service(
    service_name: str,
    sync_once: Callable[[str, str, Callable[[str], None]], str],
) -> int:
    (
        servicemanager,
        _,
        win32event,
        _,
        win32service,
        win32serviceutil,
    ) = _win32()
    config = _load_config(service_name)
    if not config:
        raise ServiceManagerError(
            f"找不到服务“{service_name}”的有效 FolderSync 配置。"
        )
    config = _validate_service_config(service_name, config)

    class FolderSyncService(win32serviceutil.ServiceFramework):
        _svc_name_ = service_name
        _svc_display_name_ = service_name
        _svc_description_ = SERVICE_MARKER

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcRun(self):
            try:
                _append_log(
                    Path(config["log_path"]),
                    "服务已启动；首次同步立即开始。",
                )
            except OSError as exc:
                servicemanager.LogErrorMsg(
                    f"FolderSync 服务日志不可写，未执行同步：{exc}"
                )
                raise
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            self.SvcDoRun()
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)

        def SvcDoRun(self):
            log_path = Path(config["log_path"])
            interval_ms = config["interval_minutes"] * 60 * 1000
            cycle_number = 0
            while True:
                wait_result = win32event.WaitForSingleObject(self.stop_event, 0)
                if wait_result == win32event.WAIT_OBJECT_0:
                    return
                if wait_result != win32event.WAIT_TIMEOUT:
                    raise ServiceManagerError("无法检查服务停止事件。")
                cycle_number += 1
                cycle_started = time.monotonic()
                try:
                    _append_log(log_path, f"第 {cycle_number} 轮同步开始。")
                    result = sync_once(
                        config["sync_path_a"],
                        config["sync_path_b"],
                        lambda message: _append_log(log_path, message),
                    )
                    if not result.startswith("完成 —"):
                        raise RuntimeError(result)
                    elapsed = time.monotonic() - cycle_started
                    _append_log(
                        log_path,
                        f"第 {cycle_number} 轮同步成功（耗时 {elapsed:.1f} 秒）：{result}",
                    )
                except Exception as exc:
                    error = f"第 {cycle_number} 轮同步失败：{exc}"
                    try:
                        _append_log(log_path, error)
                    except OSError as log_exc:
                        servicemanager.LogErrorMsg(
                            f"FolderSync 服务同步失败且日志写入失败：{log_exc}"
                        )

                remaining = interval_ms
                while remaining > 0:
                    timeout = min(remaining, _MAX_WAIT_MS)
                    wait_result = win32event.WaitForSingleObject(
                        self.stop_event, timeout
                    )
                    if wait_result == win32event.WAIT_OBJECT_0:
                        return
                    if wait_result != win32event.WAIT_TIMEOUT:
                        raise ServiceManagerError("等待下一轮同步时服务事件失败。")
                    remaining -= timeout

    try:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(FolderSyncService)
        servicemanager.StartServiceCtrlDispatcher()
    except Exception as exc:
        raise ServiceManagerError(f"Windows SCM 服务启动失败：{exc}") from exc
    return 0
