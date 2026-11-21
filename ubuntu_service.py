"""systemd user-service integration for Ubuntu."""

import getpass
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import pwd
except ImportError:
    pwd = None


SERVICE_MARKER = "FolderSync managed user service"
CONFIG_VERSION = 1
SERVICE_PREFIX = "Folder"
_SAFE_SERVICE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,120}$")


class ServiceManagerError(RuntimeError):
    """A systemd user-service operation failed."""


class ServiceStartError(ServiceManagerError):
    """A user service was installed but could not be started."""

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


def current_account_name() -> str:
    if pwd is None:
        return getpass.getuser()
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError) as exc:
        raise ServiceManagerError(f"无法获取当前 Ubuntu 用户：{exc}") from exc


def require_admin() -> None:
    return None


def _config_root() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def _state_root() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))


def _services_dir() -> Path:
    return _config_root() / "FolderSync" / "services"


def _units_dir() -> Path:
    return _config_root() / "systemd" / "user"


def _service_leaf(service_name: str) -> str:
    if not _SAFE_SERVICE_NAME.fullmatch(service_name):
        raise ServiceManagerError("服务名称无效。")
    return service_name


def _config_path(service_name: str) -> Path:
    return _services_dir() / _service_leaf(service_name) / "config.json"


def _unit_name(service_name: str) -> str:
    return f"foldersync-{_service_leaf(service_name).lower()}.service"


def _unit_path(service_name: str) -> Path:
    return _units_dir() / _unit_name(service_name)


def service_log_path(service_name: str) -> Path:
    return _state_root() / "FolderSync" / "logs" / f"{_service_leaf(service_name)}.log"


def _safe_leaf(path: str) -> str:
    leaf = Path(path.rstrip(os.sep)).name
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", leaf).strip("_")
    return safe or "Sync"


def _service_directories() -> list[Path]:
    try:
        return sorted(_services_dir().iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ServiceManagerError(
            f"无法读取 FolderSync 服务配置目录 {_services_dir()}：{exc}"
        ) from exc


def next_service_name(directory_a: str) -> str:
    base = f"{SERVICE_PREFIX}{_safe_leaf(directory_a)}"[:120]
    existing = {item.name.casefold() for item in _service_directories()}
    candidate = base
    number = 2
    while (
        candidate.casefold() in existing
        or _unit_path(candidate).exists()
    ):
        suffix = str(number)
        candidate = f"{base[:120 - len(suffix)]}{suffix}"
        number += 1
    return candidate


def _run(
    command: list[str], *, check: bool = True, timeout: int = 30
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise ServiceManagerError(
            f"找不到系统命令“{command[0]}”，请确认 systemd 工具已安装。"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ServiceManagerError(f"系统命令执行超时：{shlex.join(command)}") from exc

    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ServiceManagerError(
            f"命令失败（退出码 {result.returncode}）：{shlex.join(command)}"
            f"{f'；{detail}' if detail else ''}"
        )
    return result


def _linger_enabled(username: str) -> bool:
    result = _run(
        ["loginctl", "show-user", username, "--property=Linger", "--value"]
    )
    return result.stdout.strip().lower() == "yes"


def _manual_linger_command(username: str) -> str:
    return (
        f"可在终端执行：sudo loginctl enable-linger "
        f"{shlex.quote(username)}，然后重新创建服务。"
    )


def _ensure_linger(username: str) -> None:
    if _linger_enabled(username):
        return

    pkexec = shutil.which("pkexec")
    if not pkexec:
        raise ServiceManagerError(
            "此用户尚未启用 systemd linger，FolderSync 无法保证注销后继续运行。"
            f"请在终端执行：sudo loginctl enable-linger {shlex.quote(username)}，"
            "然后重新创建服务。"
        )
    try:
        result = subprocess.run(
            [pkexec, "loginctl", "enable-linger", username],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ServiceManagerError(
            "启用 systemd linger 的授权操作超时；没有创建服务。"
            f"{_manual_linger_command(username)}"
        ) from exc
    except OSError as exc:
        raise ServiceManagerError(
            f"无法启动系统授权对话框，未启用 linger：{exc}。"
            f"{_manual_linger_command(username)}"
        ) from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ServiceManagerError(
            "未能启用 systemd linger，服务尚未创建。"
            f"{f'系统信息：{detail}；' if detail else ''}"
            f"{_manual_linger_command(username)}"
        )
    if not _linger_enabled(username):
        raise ServiceManagerError(
            "授权命令已返回，但系统仍未启用 linger；服务尚未创建。"
            f"{_manual_linger_command(username)}"
        )


def _systemd_quote(argument: str) -> str:
    escaped = (
        argument.replace("%", "%%")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _entry_command(config_path: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-user-service", str(config_path)]
    return [
        sys.executable,
        str(Path(__file__).with_name("folder_sync.py").resolve()),
        "--run-user-service",
        str(config_path),
    ]


def _unit_contents(service_name: str, config_path: Path) -> str:
    command = " ".join(
        _systemd_quote(part) for part in _entry_command(config_path)
    )
    application_path = Path(
        sys.executable
        if getattr(sys, "frozen", False)
        else Path(__file__).with_name("folder_sync.py")
    ).resolve()
    workdir = str(application_path.parent)
    return (
        f"# {SERVICE_MARKER}: {service_name}\n"
        "[Unit]\n"
        f"Description=FolderSync periodic synchronization ({service_name})\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={_systemd_quote(workdir)}\n"
        f"Environment={_systemd_quote(f'XDG_CONFIG_HOME={_config_root()}')}\n"
        f"Environment={_systemd_quote(f'XDG_STATE_HOME={_state_root()}')}\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "Environment=PYTHONUNBUFFERED=1\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _write_private(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(contents)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _read_config(service_name: str, *, check_permissions: bool = False) -> dict:
    config_path = _config_path(service_name)
    try:
        stat = config_path.stat()
        if check_permissions:
            uid = getattr(os, "getuid", lambda: stat.st_uid)()
            unsafe_mode = os.name != "nt" and stat.st_mode & 0o077
            if stat.st_uid != uid or unsafe_mode:
                raise ServiceManagerError(
                    f"服务配置权限不安全：{config_path}。应仅允许当前用户访问。"
                )
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except ServiceManagerError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceManagerError(
            f"无法读取服务“{service_name}”的配置 {config_path}：{exc}"
        ) from exc

    if (
        not isinstance(config, dict)
        or config.get("marker") != SERVICE_MARKER
        or config.get("version") != CONFIG_VERSION
        or config.get("service_name") != service_name
        or config.get("account") != current_account_name()
        or not isinstance(config.get("directory_a"), str)
        or not isinstance(config.get("directory_b"), str)
        or not isinstance(config.get("sync_path_a"), str)
        or not isinstance(config.get("sync_path_b"), str)
        or not Path(config["sync_path_a"]).is_absolute()
        or not Path(config["sync_path_b"]).is_absolute()
        or not isinstance(config.get("interval_minutes"), int)
        or isinstance(config.get("interval_minutes"), bool)
        or config["interval_minutes"] <= 0
        or config.get("log_path") != str(service_log_path(service_name))
    ):
        raise ServiceManagerError(f"服务“{service_name}”的配置无效。")
    return config


def _verify_managed_service(service_name: str) -> tuple[dict, Path]:
    config = _read_config(service_name)
    unit_path = _unit_path(service_name)
    try:
        unit = unit_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ServiceManagerError(
            f"无法读取服务 unit 文件 {unit_path}：{exc}"
        ) from exc
    if (
        f"# {SERVICE_MARKER}: {service_name}" not in unit
        or _systemd_quote(str(_config_path(service_name))) not in unit
    ):
        raise ServiceManagerError(
            f"服务 unit“{_unit_name(service_name)}”无法验证为 FolderSync 创建，"
            "为保护其他服务，未执行操作。"
        )
    return config, unit_path


def _status(service_name: str) -> str:
    result = _run(
        ["systemctl", "--user", "show", _unit_name(service_name),
         "--property=ActiveState", "--value"],
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ServiceManagerError(
            f"无法查询服务“{service_name}”状态：{detail or result.returncode}"
        )
    return {
        "active": "运行中",
        "inactive": "已停止",
        "failed": "启动失败",
        "activating": "启动中",
        "deactivating": "停止中",
    }.get(result.stdout.strip(), result.stdout.strip() or "未知")


def list_managed_services() -> list[ManagedService]:
    services = []
    for service_dir in _service_directories():
        if not service_dir.is_dir():
            continue
        service_name = service_dir.name
        config, _ = _verify_managed_service(service_name)
        services.append(
            ManagedService(
                name=service_name,
                directory_a=config["directory_a"],
                directory_b=config["directory_b"],
                interval_minutes=config["interval_minutes"],
                status=_status(service_name),
                account=config["account"],
                binary_path=" ".join(
                    _systemd_quote(part)
                    for part in _entry_command(_config_path(service_name))
                ),
                log_path=str(service_log_path(service_name)),
            )
        )
    return services


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
    interval = validate_interval(interval_minutes)
    username = current_account_name()
    if account != username:
        raise ServiceManagerError(
            "Ubuntu 用户服务只能以当前用户身份运行；服务不会保存或使用用户密码。"
        )
    if password:
        raise ServiceManagerError("Ubuntu 服务不接受或保存账户密码。")
    for label, path in (("A", sync_path_a), ("B", sync_path_b)):
        try:
            directory_status = Path(path).stat()
        except OSError as exc:
            raise ServiceManagerError(
                f"目录 {label} 当前不可访问：{path}（{exc}）。"
                "请先挂载网络共享并完成一次同步后重试。"
            ) from exc
        if not stat.S_ISDIR(directory_status.st_mode):
            raise ServiceManagerError(
                f"目录 {label} 不是可访问的目录：{path}。"
                "请先挂载网络共享并完成一次同步后重试。"
            )
    expected_name = next_service_name(directory_a)
    if service_name != expected_name:
        raise ServiceManagerError(
            f"服务名已被占用，请刷新后使用新服务名“{expected_name}”。"
        )

    _ensure_linger(username)
    config_path = _config_path(service_name)
    unit_path = _unit_path(service_name)
    log_path = service_log_path(service_name)
    config = {
        "marker": SERVICE_MARKER,
        "version": CONFIG_VERSION,
        "service_name": service_name,
        "directory_a": directory_a,
        "directory_b": directory_b,
        "sync_path_a": sync_path_a,
        "sync_path_b": sync_path_b,
        "interval_minutes": interval,
        "account": username,
        "log_path": str(log_path),
    }

    config_created = False
    unit_created = False
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(config_path.parent, 0o700)
        _write_private(config_path, json.dumps(config, ensure_ascii=False, indent=2))
        config_created = True
        _write_private(unit_path, _unit_contents(service_name, config_path))
        unit_created = True
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(log_path.parent, 0o700)
        descriptor = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except OSError as exc:
        cleanup_errors = []
        for path, created in ((unit_path, unit_created), (config_path, config_created)):
            if created:
                try:
                    path.unlink()
                except OSError as cleanup_error:
                    cleanup_errors.append(f"{path}: {cleanup_error}")
        details = (
            f"；清理未完成：{'; '.join(cleanup_errors)}"
            if cleanup_errors else ""
        )
        raise ServiceManagerError(
            f"创建服务文件失败：{exc}{details}"
        ) from exc

    try:
        _run(["systemctl", "--user", "daemon-reload"])
    except ServiceManagerError as exc:
        raise ServiceStartError(
            service_name,
            f"服务配置已创建，但 systemd 重新加载失败：{exc}\n"
            f"配置：{config_path}\n日志：{log_path}",
        ) from exc
    try:
        _run(
            ["systemctl", "--user", "enable", "--now", _unit_name(service_name)]
        )
    except ServiceManagerError as exc:
        raise ServiceStartError(
            service_name,
            f"服务配置已创建，但启用或启动失败：{exc}\n"
            f"配置：{config_path}\n日志：{log_path}",
        ) from exc


def start_managed_service(service_name: str, password: str = "") -> bool:
    if password:
        raise ServiceManagerError("Ubuntu 服务不接受或保存账户密码。")
    _verify_managed_service(service_name)
    if _status(service_name) == "运行中":
        return False
    _run(["systemctl", "--user", "start", _unit_name(service_name)])
    return True


def delete_service(service_name: str) -> None:
    _, unit_path = _verify_managed_service(service_name)
    _run(
        ["systemctl", "--user", "disable", "--now", _unit_name(service_name)]
    )
    _run(["systemctl", "--user", "daemon-reload"])
    config_path = _config_path(service_name)
    config_dir = config_path.parent
    try:
        unit_path.unlink()
        config_path.unlink()
        config_dir.rmdir()
    except OSError as exc:
        raise ServiceManagerError(
            f"服务已停止并禁用，但清理服务文件失败：{exc}"
        ) from exc
    log_path = service_log_path(service_name)
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_path.parent, 0o700)
    _run(["systemctl", "--user", "daemon-reload"])


def build_service_start_command(service_name: str) -> str:
    return f"systemctl --user start {shlex.quote(_unit_name(service_name))}"


def build_service_command_text(service: ManagedService) -> str:
    unit = _unit_name(service.name)
    return (
        f"服务进程入口（systemd user unit）：\n{service.binary_path}\n\n"
        f"运行账户：{service.account}\n\n"
        "排错命令：\n"
        f"{build_service_start_command(service.name)}\n"
        f"systemctl --user status {shlex.quote(unit)}\n"
        f"journalctl --user -u {shlex.quote(unit)} -n 80 --no-pager\n"
        f"tail -n 80 {shlex.quote(service.log_path)}"
    )


def _append_log(log_path: Path, message: str) -> None:
    try:
        descriptor = os.open(
            log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600
        )
        with os.fdopen(descriptor, "a", encoding="utf-8") as log_file:
            log_file.write(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
            )
    except OSError as exc:
        raise ServiceManagerError(
            f"无法写入 FolderSync 服务日志 {log_path}：{exc}"
        ) from exc


def run_user_service(
    config_path: str,
    sync_once: Callable[[str, str, Callable[[str], None]], str],
) -> int:
    path = Path(config_path)
    if path != _config_path(path.parent.name):
        raise ServiceManagerError("服务配置路径不符合 FolderSync 目录规则。")
    config = _read_config(path.parent.name, check_permissions=True)
    log_path = Path(config["log_path"])
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    _append_log(log_path, "服务已启动；首次同步立即开始。")
    cycle = 0
    interval_seconds = config["interval_minutes"] * 60
    while not stopping:
        cycle += 1
        _append_log(log_path, f"第 {cycle} 轮同步开始。")
        try:
            result = sync_once(
                config["sync_path_a"],
                config["sync_path_b"],
                lambda message: _append_log(log_path, message),
            )
            if not result.startswith("完成 —"):
                raise RuntimeError(result)
            _append_log(log_path, f"第 {cycle} 轮同步成功：{result}")
        except Exception as exc:
            _append_log(log_path, f"第 {cycle} 轮同步失败：{exc}")

        deadline = time.monotonic() + interval_seconds
        while not stopping:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            pause = min(remaining, 1)
            time.sleep(pause)

    _append_log(log_path, "服务已停止。")
    return 0


def run_service(
    config_path: str,
    sync_once: Callable[[str, str, Callable[[str], None]], str],
) -> int:
    return run_user_service(config_path, sync_once)
