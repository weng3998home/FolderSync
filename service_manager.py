"""Platform-neutral service operations used by the FolderSync UI."""

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

if os.name == "nt":
    _platform_backend_name = "windows_service"
elif sys.platform.startswith("linux"):
    _platform_backend_name = "ubuntu_service"
else:
    _platform_backend_name = None


class ServiceManagerError(RuntimeError):
    """A platform service operation failed."""


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


def is_windows() -> bool:
    return os.name == "nt"


def is_ubuntu() -> bool:
    return sys.platform.startswith("linux")


def requires_account_password() -> bool:
    return is_windows()


def _backend():
    if _platform_backend_name is None:
        raise ServiceManagerError("FolderSync 服务仅支持 Windows 和 Ubuntu。")
    try:
        return importlib.import_module(_platform_backend_name)
    except ImportError as exc:
        raise ServiceManagerError(
            f"无法加载当前平台的服务实现 {_platform_backend_name}：{exc}"
        ) from exc


def _call(name: str, *args, **kwargs):
    backend = _backend()
    try:
        return getattr(backend, name)(*args, **kwargs)
    except getattr(backend, "ServiceStartError", ()) as exc:
        raise ServiceStartError(exc.service_name, str(exc)) from exc
    except backend.ServiceManagerError as exc:
        raise ServiceManagerError(str(exc)) from exc


def validate_interval(value: str | int) -> int:
    return _call("validate_interval", value)


def require_admin() -> None:
    return _call("require_admin")


def current_account_name() -> str:
    return _call("current_account_name")


def next_service_name(directory_a: str) -> str:
    return _call("next_service_name", directory_a)


def service_log_path(service_name: str) -> Path:
    return _call("service_log_path", service_name)


def list_managed_services() -> list[ManagedService]:
    services = _call("list_managed_services")
    return [
        ManagedService(
            name=service.name,
            directory_a=service.directory_a,
            directory_b=service.directory_b,
            interval_minutes=service.interval_minutes,
            status=service.status,
            account=service.account,
            binary_path=service.binary_path,
            log_path=str(service.log_path),
        )
        for service in services
    ]


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
    return _call(
        "create_service",
        directory_a,
        directory_b,
        sync_path_a,
        sync_path_b,
        interval_minutes,
        account,
        password,
        service_name,
    )


def start_managed_service(service_name: str, password: str = "") -> bool:
    return _call("start_managed_service", service_name, password)


def delete_service(service_name: str) -> None:
    return _call("delete_service", service_name)


def run_service(
    service_name_or_config: str,
    sync_once,
) -> int:
    if is_windows():
        return _call("run_service", service_name_or_config, sync_once)
    if is_ubuntu():
        return _call("run_user_service", service_name_or_config, sync_once)
    raise ServiceManagerError("FolderSync 服务仅支持 Windows 和 Ubuntu。")


def build_service_start_command(service_name: str) -> str:
    if is_windows():
        return f"sc.exe start {_quote_powershell(service_name)}"
    return _call("build_service_start_command", service_name)


def build_service_command_text(service: ManagedService) -> str:
    if is_windows():
        return (
            f"服务进程入口（Windows SCM 配置）：\n{service.binary_path}\n\n"
            f"运行账户：{service.account}\n\n"
            "PowerShell 排错命令：\n"
            f"{build_service_start_command(service.name)}\n"
            f"sc.exe qc {_quote_powershell(service.name)}\n"
            f"sc.exe queryex {_quote_powershell(service.name)}\n"
            f"Get-Content {_quote_powershell(service.log_path)} -Tail 80"
        )
    return _call("build_service_command_text", service)


def _quote_powershell(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
