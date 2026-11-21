"""
Folder Sync - 目录同步工具
比较两个目录（含子目录）的文件，按修改日期更新。
特殊规则：文件或目录名以 DEL 开头的，会删除对面目录中对应的文件或目录，然后删除自己。
"""

import errno
import ctypes
import re
import stat
import sys
import os
import shlex
import shutil
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlparse
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QTableWidgetItem, QVBoxLayout
)
from PyQt6.QtCore import QCoreApplication, QThread, Qt, pyqtSignal

# 导入 Qt Designer 生成的 UI 类
from main_window_ui import Ui_MainWindow
import service_manager


_IGNORED_SYNC_FILES = {
    "thumbs.db",
    "ehthumbs.db",
    "desktop.ini",
    "pagefile.sys",
    "hiberfil.sys",
    "swapfile.sys",
}
_IGNORED_SYNC_DIRECTORIES = {
    "$recycle.bin",
    "system volume information",
}


def _is_ignored_sync_entry(name: str, is_directory: bool) -> bool:
    normalized_name = name.casefold()
    if is_directory:
        return normalized_name in _IGNORED_SYNC_DIRECTORIES
    return (
        normalized_name in _IGNORED_SYNC_FILES
        or normalized_name.endswith((".tmp", ".temp", ".lnk"))
    )


def _is_ubuntu():
    """检测当前是否在 Ubuntu/Linux 桌面环境"""
    return sys.platform.startswith("linux")


# ──────────────────────────────────────────────
# GVFS ↔ URL 路径转换
# ──────────────────────────────────────────────

def _gvfs_to_url(gvfs_path: str) -> str | None:
    """将 GVFS 的 SMB 挂载路径转换为用于界面显示的 smb:// URL。"""
    try:
        result = subprocess.run(
            ["gio", "info", "-a", "standard::target-uri", gvfs_path],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                marker = "standard::target-uri:"
                if marker in line:
                    url = line.partition(marker)[2].strip()
                    if url.startswith("smb://"):
                        return url
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    mount_marker = "/gvfs/smb-share:"
    if mount_marker not in gvfs_path:
        return None

    mount_info = gvfs_path.split(mount_marker, 1)[1]
    fields, _, sub_path = mount_info.partition("/")
    values = {}
    for field in fields.split(","):
        key, separator, value = field.partition("=")
        if separator:
            values[key] = value

    server = values.get("server")
    share = values.get("share")
    if server and share:
        return f"smb://{server}/{share}" + (f"/{sub_path}" if sub_path else "")
    return None


def _url_to_gvfs(url: str) -> str | None:
    """将已挂载共享的 smb:// URL 转换为对应 GVFS 本地路径。"""
    parsed = urlparse(url)
    if parsed.scheme.lower() != "smb" or not parsed.hostname:
        return None

    path_parts = [unquote(part) for part in parsed.path.split("/") if part]
    if not path_parts:
        return None

    share, *sub_path = path_parts
    gvfs_path = (
        Path(f"/run/user/{os.getuid()}/gvfs")
        / f"smb-share:server={parsed.hostname},share={share}"
        / Path(*sub_path)
    )
    if gvfs_path.is_dir():
        return str(gvfs_path)
    return None


def _url_to_windows_unc(url: str) -> str | None:
    """将 smb:// URL 转换为 Windows UNC 网络路径。"""
    parsed = urlparse(url)
    if parsed.scheme.lower() != "smb" or not parsed.hostname:
        return None

    path_parts = [unquote(part) for part in parsed.path.split("/") if part]
    if not path_parts:
        return None

    return str(PureWindowsPath(
        f"\\\\{parsed.hostname}\\{path_parts[0]}",
        *path_parts[1:]
    ))


def _normalize_path(raw: str) -> str:
    """将 GVFS 挂载路径转换为界面显示的 URL；普通路径保持不变。"""
    if "/gvfs/" in raw:
        return _gvfs_to_url(raw) or raw
    return raw


def _to_local_path(path_str: str) -> str:
    """将 smb:// URL 转换为当前平台的网络路径；其他路径保持不变。"""
    if path_str.lower().startswith("smb://"):
        if os.name == "nt":
            return _url_to_windows_unc(path_str) or path_str
        return _url_to_gvfs(path_str) or path_str
    return path_str


def _resolve_sync_paths(dir_a: str, dir_b: str) -> tuple[str, str]:
    """统一解析并验证 UI 和命令行提供的两个目录。"""
    if not dir_a.strip() or not dir_b.strip():
        raise ValueError("请提供目录 A 和目录 B。")

    local_a = _to_local_path(dir_a)
    local_b = _to_local_path(dir_b)

    if not Path(local_a).is_dir():
        raise ValueError(
            f"目录 A 不存在或无法访问:\n{dir_a}\n\n"
            "如果是网络路径，请先在文件管理器中挂载该共享。"
        )
    if not Path(local_b).is_dir():
        raise ValueError(
            f"目录 B 不存在或无法访问:\n{dir_b}\n\n"
            "如果是网络路径，请先在文件管理器中挂载该共享。"
        )
    if Path(local_a).resolve() == Path(local_b).resolve():
        raise ValueError("目录 A 和目录 B 不能是同一个目录。")

    return local_a, local_b


# ──────────────────────────────────────────────
# 目录选择对话框
# ──────────────────────────────────────────────

def _pick_folder_zenity(title: str, start_dir: str = "") -> str | None:
    """
    调用 zenity（Ubuntu 自带的 GTK 对话框）选择目录。
    它的界面和 Ubuntu 文件管理器一样，左侧有"网络"选项，
    可以直接浏览和选择 SMB 网络共享。
    网络共享路径会转换为 smb:// URL 格式显示。
    """
    cmd = [
        "zenity",
        "--file-selection",
        "--directory",
        f"--title={title}",
    ]
    # start_dir 可能是 smb:// URL，zenity 需要本地路径
    actual_start = _to_local_path(start_dir)
    if actual_start and Path(actual_start).is_dir():
        cmd.append(f"--filename={actual_start}")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            raw_path = result.stdout.strip()
            if raw_path:
                return _normalize_path(raw_path)
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _pick_folder_qt(title: str, start_dir: str = "") -> str | None:
    """回退方案：使用 Qt 内置的文件对话框。"""
    actual_start = _to_local_path(start_dir)
    dir_path = QFileDialog.getExistingDirectory(
        None, title, actual_start or str(Path.home())
    )
    return dir_path if dir_path else None


def pick_folder(title: str, start_dir: str = "") -> str | None:
    """
    智能选择目录：优先用 zenity（支持网络浏览），
    不可用时回退到 Qt 内置对话框。
    """
    if _is_ubuntu():
        result = _pick_folder_zenity(title, start_dir)
        if result is not None:
            return result
    return _pick_folder_qt(title, start_dir)


def _build_sync_command(dir_a: str, dir_b: str) -> str:
    """生成当前平台可直接执行的命令行。"""
    if os.name == "nt":
        if getattr(sys, "frozen", False):
            arguments = [sys.executable, dir_a, dir_b]
        else:
            arguments = [
                sys.executable,
                str(Path(__file__).resolve()),
                dir_a,
                dir_b,
            ]
        return subprocess.list2cmdline(arguments)

    return f"./folder_sync {shlex.quote(dir_a)} {shlex.quote(dir_b)}"


def _build_service_command_text(service) -> str:
    """生成所选平台服务的入口、状态检查和日志查看命令。"""
    return service_manager.build_service_command_text(service)


def _build_service_start_command(service_name: str) -> str:
    """生成当前平台的服务启动命令。"""
    return service_manager.build_service_start_command(service_name)


def _run_sync_once(
    dir_a: str, dir_b: str, log_callback
) -> str:
    """Run one existing synchronization round without starting a Qt event loop."""
    local_a, local_b = _resolve_sync_paths(dir_a, dir_b)
    worker = SyncWorker(local_a, local_b)
    worker.log.connect(log_callback)
    result = []
    worker.finished_signal.connect(result.append)
    worker.run()
    if not result:
        raise RuntimeError("同步轮次未返回结果。")
    if not result[0].startswith("完成 —"):
        raise RuntimeError(result[0])
    return result[0]


def _is_mapped_drive(path: str) -> bool:
    if os.name != "nt":
        return False
    normalized_path = path.replace("/", "\\")
    match = re.match(r"^([A-Za-z]:)\\", normalized_path)
    if not match:
        return False
    return ctypes.windll.kernel32.GetDriveTypeW(
        match.group(1) + "\\"
    ) == 4


# ──────────────────────────────────────────────
# 同步工作线程
# ──────────────────────────────────────────────

class SyncWorker(QThread):
    """后台同步线程，避免阻塞 UI"""
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished_signal = pyqtSignal(str)

    def __init__(self, dir_a: str, dir_b: str):
        """dir_a, dir_b 应该是本地可访问的文件系统路径"""
        super().__init__()
        self.dir_a = Path(dir_a)
        self.dir_b = Path(dir_b)
        self._had_errors = False

    def _record_error(self, operation: str, path: Path, error: OSError):
        self._had_errors = True
        detail = error.strerror or str(error)
        self.log.emit(
            f"  [操作失败] {operation}: {path} ({detail}; errno={error.errno})"
        )

    def _scan(self, root: Path) -> tuple[dict[str, Path], dict[str, Path], bool]:
        files = {}
        dirs = {}
        complete = True

        try:
            with os.scandir(root):
                pass
        except OSError as exc:
            self._record_error("无法读取目录", root, exc)
            return files, dirs, False

        def on_error(error):
            nonlocal complete
            complete = False
            self._record_error("无法扫描目录", Path(error.filename or root), error)

        for current, directory_names, file_names in os.walk(
            root, topdown=True, onerror=on_error
        ):
            current_path = Path(current)
            included_directories = [
                name for name in directory_names
                if not _is_ignored_sync_entry(name, is_directory=True)
            ]
            directory_names[:] = included_directories
            for name in included_directories:
                path = current_path / name
                dirs[path.relative_to(root).as_posix()] = path

            for name in file_names:
                if _is_ignored_sync_entry(name, is_directory=False):
                    continue
                path = current_path / name
                files[path.relative_to(root).as_posix()] = path

        return files, dirs, complete

    def get_all_files(self, root: Path) -> dict:
        """
        递归扫描目录，返回 {相对路径: 绝对路径} 的字典。
        相对路径统一用正斜杠，保证跨平台比较一致。
        """
        return self._scan(root)[0]

    def get_all_dirs(self, root: Path) -> dict:
        """
        递归扫描目录，返回 {相对路径: 绝对路径} 的字典（仅目录）。
        """
        return self._scan(root)[1]

    def _copy_file(self, src: Path, dst: Path, rel: str):
        """复制文件并保留时间戳；目标文件系统不支持权限操作时，仅复制内容和时间戳。"""
        try:
            shutil.copy2(str(src), str(dst))
        except OSError as exc:
            unsupported_errors = {errno.EOPNOTSUPP, errno.ENOTSUP}
            if exc.errno not in unsupported_errors:
                raise

            self.log.emit(f"  [提示] 目标文件系统不支持权限复制，改为仅复制内容: {rel}")
            source_stat = src.stat()
            with src.open("rb") as source_file, dst.open("wb") as destination_file:
                shutil.copyfileobj(source_file, destination_file)
            os.utime(
                dst,
                ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns)
            )

    @staticmethod
    def _del_directory_exclusions(
        *directory_maps: dict[str, Path]
    ) -> set[str]:
        exclusions = set()
        for directories in directory_maps:
            for rel in directories:
                marker_name = PurePosixPath(rel).name
                if not marker_name.upper().startswith("DEL"):
                    continue
                exclusions.add(rel)
                target_name = marker_name[3:]
                if not target_name:
                    continue
                parent = PurePosixPath(rel).parent
                exclusions.add((parent / target_name).as_posix())
        return exclusions

    @staticmethod
    def _del_file_exclusions(
        files_a: dict[str, Path], files_b: dict[str, Path]
    ) -> tuple[set[str], set[str]]:
        markers = set()
        targets = set()
        for rel in set(files_a) | set(files_b):
            marker_name = PurePosixPath(rel).name
            if not marker_name.upper().startswith("DEL"):
                continue
            markers.add(rel)
            if rel in files_a and rel in files_b:
                continue
            target_name = marker_name[3:]
            if target_name:
                targets.add(
                    (PurePosixPath(rel).parent / target_name).as_posix()
                )
        return markers, targets

    @staticmethod
    def _is_under_excluded_directory(rel: str, exclusions: set[str]) -> bool:
        current = PurePosixPath(rel)
        while current != PurePosixPath("."):
            if current.as_posix() in exclusions:
                return True
            current = current.parent
        return False

    def _process_del_dirs(
        self, root: Path, opposite_root: Path, dirs: dict[str, Path]
    ) -> int:
        """
        处理 root 下所有以 DEL 开头的目录：
        1. 在 opposite_root 中找到对应目录并递归删除
        2. 删除 DEL 目录自身
        返回删除数量。
        """
        deleted = 0
        # 按路径长度降序排列，先处理深层目录
        for rel in sorted(dirs.keys(), key=len, reverse=True):
            name = PurePosixPath(rel).name
            if not name.upper().startswith("DEL"):
                continue

            target_name = name[3:]
            if not target_name:
                self.log.emit(f"  [跳过] DEL 目录名缺少目标名称: {rel}")
                continue
            target_rel = (PurePosixPath(rel).parent / target_name).as_posix()
            target_path = opposite_root / target_rel
            del_path = dirs[rel]

            try:
                target_status = target_path.stat()
            except FileNotFoundError:
                target_exists = False
            except OSError as exc:
                self._record_error("无法检查删除目标", target_path, exc)
                continue
            else:
                target_exists = stat.S_ISDIR(target_status.st_mode)

            if target_exists:
                try:
                    shutil.rmtree(str(target_path))
                    self.log.emit(f"  [删除目录] {target_rel}")
                    deleted += 1
                except OSError as exc:
                    self._record_error("删除目录失败", target_path, exc)
                    continue

            try:
                shutil.rmtree(str(del_path))
                self.log.emit(f"  [删除目录] DEL 指令目录 {rel}")
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._record_error("删除 DEL 指令目录失败", del_path, exc)

        return deleted

    def _process_del_files(
        self,
        root_a: Path,
        root_b: Path,
        files_a: dict[str, Path],
        files_b: dict[str, Path],
    ) -> tuple[int, set[str]]:
        """处理两边以 DEL 开头的文件，返回删除数和本轮不再同步的目标路径。"""
        deleted = 0
        skipped_paths = set()

        for rel in sorted(set(files_a) | set(files_b)):
            name = PurePosixPath(rel).name
            if not name.upper().startswith("DEL"):
                continue

            in_a = rel in files_a
            in_b = rel in files_b
            if in_a and in_b:
                self.log.emit(f"  [跳过] 两边都有 {rel}，忽略")
                continue

            target_name = name[3:]
            if not target_name:
                self.log.emit(f"  [跳过] DEL 文件名缺少目标名称: {rel}")
                continue

            target_rel = (PurePosixPath(rel).parent / target_name).as_posix()
            skipped_paths.add(target_rel)
            source_root, opposite_root = (
                (root_a, root_b) if in_a else (root_b, root_a)
            )
            del_path = source_root / rel

            target_path = opposite_root / target_rel
            try:
                target_status = target_path.stat()
            except FileNotFoundError:
                target_exists = False
            except OSError as exc:
                self._record_error("无法检查删除目标", target_path, exc)
                continue
            else:
                target_exists = stat.S_ISREG(target_status.st_mode)

            if target_exists:
                try:
                    target_path.unlink()
                except OSError as exc:
                    self._record_error("删除文件失败", target_path, exc)
                    continue
                self.log.emit(f"  [删除文件] {target_rel}")
                deleted += 1
                self._remove_empty_parents(target_path.parent, opposite_root)

            try:
                del_path.unlink()
                self.log.emit(f"  [删除文件] DEL 指令文件 {rel}")
                self._remove_empty_parents(del_path.parent, source_root)
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._record_error("删除 DEL 指令文件失败", del_path, exc)

        return deleted, skipped_paths

    def run(self):
        try:
            self._had_errors = False
            self.log.emit(f"扫描目录 A: {self.dir_a}")
            self.log.emit(f"扫描目录 B: {self.dir_b}")

            copied = 0
            deleted = 0

            files_a, dirs_a, complete_a = self._scan(self.dir_a)
            self.log.emit(f"  目录 A 找到 {len(files_a)} 个文件")
            files_b, dirs_b, complete_b = self._scan(self.dir_b)
            self.log.emit(f"  目录 B 找到 {len(files_b)} 个文件")
            directory_exclusions = self._del_directory_exclusions(
                dirs_a, dirs_b
            )
            file_marker_exclusions, del_file_targets = (
                self._del_file_exclusions(files_a, files_b)
            )

            self.log.emit("\n第一阶段：处理 DEL 删除指令...\n")
            if complete_a and complete_b:
                del_a = self._process_del_dirs(self.dir_a, self.dir_b, dirs_a)
                del_b = self._process_del_dirs(self.dir_b, self.dir_a, dirs_b)
                del_files, del_file_targets = self._process_del_files(
                    self.dir_a, self.dir_b, files_a, files_b
                )
                deleted += del_a + del_b + del_files
                if del_a + del_b + del_files == 0:
                    self.log.emit("  无 DEL 删除指令")
            else:
                self._had_errors = True
                self.log.emit(
                    "  [跳过] 至少一个目录扫描不完整；本轮不执行任何 DEL 删除指令。"
                )

            self.log.emit("\n第二阶段：扫描文件并同步...\n")
            all_rel_paths = (
                set(files_a.keys()) | set(files_b.keys())
            ) - del_file_targets
            total = len(all_rel_paths)
            processed = 0

            self.log.emit(f"\n共 {total} 个文件需要比较，开始同步...\n")

            for rel in sorted(all_rel_paths):
                processed += 1
                if total > 0:
                    self.progress.emit(int(processed / total * 100))

                if (
                    rel in file_marker_exclusions
                    or rel in del_file_targets
                    or self._is_under_excluded_directory(
                        rel, directory_exclusions
                    )
                ):
                    continue

                in_a = rel in files_a
                in_b = rel in files_b

                # ── 正常文件同步逻辑 ──
                if in_a and not in_b:
                    if not complete_b:
                        self._had_errors = True
                        self.log.emit(
                            f"  [跳过] 目录 B 扫描不完整，无法确认目标是否存在: {rel}"
                        )
                        continue
                    src = files_a[rel]
                    dst = self.dir_b / rel
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        self._copy_file(src, dst, rel)
                    except OSError as exc:
                        self._record_error(
                            f"A→B 复制失败（源文件 {src}）", dst, exc
                        )
                    else:
                        self.log.emit(f"  [A→B] {rel}")
                        copied += 1

                elif in_b and not in_a:
                    if not complete_a:
                        self._had_errors = True
                        self.log.emit(
                            f"  [跳过] 目录 A 扫描不完整，无法确认目标是否存在: {rel}"
                        )
                        continue
                    src = files_b[rel]
                    dst = self.dir_a / rel
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        self._copy_file(src, dst, rel)
                    except OSError as exc:
                        self._record_error(
                            f"B→A 复制失败（源文件 {src}）", dst, exc
                        )
                    else:
                        self.log.emit(f"  [B→A] {rel}")
                        copied += 1

                else:
                    error_path = files_a[rel]
                    error_operation = "读取文件 A"
                    try:
                        mtime_a = files_a[rel].stat().st_mtime
                        error_path = files_b[rel]
                        error_operation = "读取文件 B"
                        mtime_b = files_b[rel].stat().st_mtime

                        if abs(mtime_a - mtime_b) < 1.0:
                            continue

                        if mtime_a > mtime_b:
                            src = files_a[rel]
                            dst = self.dir_b / rel
                            direction = "A→B 更新"
                        else:
                            src = files_b[rel]
                            dst = self.dir_a / rel
                            direction = "B→A 更新"
                        error_path = dst
                        error_operation = f"{direction} 复制（源文件 {src}）"
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        self._copy_file(src, dst, rel)
                    except OSError as exc:
                        self._record_error(error_operation, error_path, exc)
                    else:
                        self.log.emit(f"  [{direction}] {rel}")
                        copied += 1

            outcome = "部分完成" if self._had_errors else "同步完成"
            summary = (
                f"\n{'='*40}\n"
                f"{outcome}！\n"
                f"  复制: {copied} 个文件\n"
                f"  删除: {deleted} 个文件/目录\n"
                f"{'='*40}"
            )
            self.log.emit(summary)
            self.finished_signal.emit(
                f"{'部分完成' if self._had_errors else '完成'} — "
                f"复制 {copied} 个，删除 {deleted} 个"
                f"{'；有操作失败或目录扫描不完整，请查看日志' if self._had_errors else ''}"
            )

        except Exception as e:
            self.log.emit(f"\n[错误] {e}")
            self.finished_signal.emit(f"出错: {e}")

    def _remove_empty_parents(self, dir_path: Path, stop_at: Path):
        """向上递归删除空目录，直到 stop_at"""
        while dir_path != stop_at:
            try:
                with os.scandir(dir_path) as entries:
                    if next(entries, None) is not None:
                        break
                dir_path.rmdir()
            except NotADirectoryError:
                break
            except FileNotFoundError:
                break
            except OSError as exc:
                self._record_error("清理空目录失败", dir_path, exc)
                break
            self.log.emit(f"  [清理] 空目录 {dir_path.relative_to(stop_at)}")
            dir_path = dir_path.parent


# ──────────────────────────────────────────────
# 主窗口
# ──────────────────────────────────────────────

class _PasswordDialog(QDialog):
    def __init__(
        self,
        account: str,
        parent=None,
        title: str = "确认服务账户",
        submit_text: str = "创建并启动",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"请输入 Windows 账户 {account} 的密码。\n"
            "密码仅用于更新 Windows 服务控制管理器中的账户凭据，"
            "不会保存到文件或日志。"
        ))
        self.password_edit = QLineEdit(self)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("Windows 账户密码")
        layout.addWidget(self.password_edit)
        buttons = QHBoxLayout()
        self.cancel_button = QPushButton("取消", self)
        self.create_button = QPushButton(submit_text, self)
        self.cancel_button.clicked.connect(self.reject)
        self.create_button.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.create_button)
        layout.addLayout(buttons)


class MainWindow(QMainWindow, Ui_MainWindow):
    def __init__(self):
        super().__init__()
        self.setupUi(self)
        icon_name = "foldersync.ico" if os.name == "nt" else "foldersync.png"
        icon_path = os.path.join(
            getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
            icon_name,
        )
        self.setWindowIcon(QIcon(icon_path))
        self.worker = None
        self._sync_command_args = None
        self._successful_sync_dirs = None
        self._managed_services = {}

        # 连接信号
        self.btn_browse_a.clicked.connect(self._browse_dir_a)
        self.btn_browse_b.clicked.connect(self._browse_dir_b)
        self.btn_sync.clicked.connect(self._start_sync)
        self.btn_copy_command.clicked.connect(self._copy_command)
        self.btn_copy_service_command.clicked.connect(
            self._copy_service_command
        )
        self.btn_create_service.clicked.connect(self._create_service)
        self.btn_start_service.clicked.connect(self._start_selected_service)
        self.btn_refresh_services.clicked.connect(self._refresh_services)
        self.btn_delete_service.clicked.connect(self._delete_selected_service)
        self.service_table.itemSelectionChanged.connect(
            self._update_selected_service
        )
        self.btn_create_service.setEnabled(False)
        self.btn_delete_service.setEnabled(False)
        supported_platform = (
            service_manager.is_windows() or service_manager.is_ubuntu()
        )
        self.service_group.setVisible(supported_platform)
        if service_manager.is_ubuntu():
            self.service_group.setTitle("Ubuntu 用户定时同步服务")
            self.btn_start_service.setText("启动服务")
            self.label_service_note.setText(
                "服务以当前用户身份运行，不需要输入密码。首次创建时会请求管理员授权"
                "启用 systemd linger，使服务在注销后继续运行并在重启后自动启动。"
                "服务只拥有当前用户已有的文件权限；GVFS/SMB 共享未挂载时本轮会记录"
                "失败并在后续周期重试。"
            )

        self.statusbar.showMessage("就绪 — 请选择两个目录")
        if supported_platform:
            self._refresh_services()

    def _browse_dir_a(self):
        dir_path = pick_folder("选择目录 A", self.edit_dir_a.text())
        if dir_path:
            self.edit_dir_a.setText(dir_path)
            self._update_service_eligibility()
            self.statusbar.showMessage(f"目录 A: {dir_path}")

    def _browse_dir_b(self):
        dir_path = pick_folder("选择目录 B", self.edit_dir_b.text())
        if dir_path:
            self.edit_dir_b.setText(dir_path)
            self._update_service_eligibility()
            self.statusbar.showMessage(f"目录 B: {dir_path}")

    def _update_service_eligibility(self):
        self.btn_create_service.setEnabled(
            (service_manager.is_windows() or service_manager.is_ubuntu())
            and self._successful_sync_dirs is not None
            and (self.edit_dir_a.text(), self.edit_dir_b.text())
            == self._successful_sync_dirs
        )

    def _start_sync(self):
        display_a = self.edit_dir_a.text()
        display_b = self.edit_dir_b.text()
        try:
            local_a, local_b = _resolve_sync_paths(display_a, display_b)
        except ValueError as exc:
            QMessageBox.warning(self, "目录无效", str(exc))
            return

        # 确认（显示友好的路径格式）
        reply = QMessageBox.question(
            self, "确认同步",
            f"即将同步以下两个目录：\n\n"
            f"目录 A: {display_a}\n"
            f"目录 B: {display_b}\n\n"
            f"较新的文件会覆盖较旧的，DEL 开头的文件会触发删除。\n"
            f"确定开始？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # 清空日志，禁用按钮
        self._successful_sync_dirs = None
        self.btn_create_service.setEnabled(False)
        self.log_output.clear()
        self.edit_command.clear()
        self._sync_command_args = (display_a, display_b)
        self.btn_sync.setEnabled(False)
        self.btn_browse_a.setEnabled(False)
        self.btn_browse_b.setEnabled(False)
        self.progress_bar.setValue(0)
        self.statusbar.showMessage("同步中...")

        # 启动后台线程（传入本地可访问路径）
        self.worker = SyncWorker(local_a, local_b)
        self.worker.log.connect(self._on_log)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.start()

    def _on_log(self, msg: str):
        self.log_output.appendPlainText(msg)

    def _on_progress(self, value: int):
        self.progress_bar.setValue(value)

    def _on_finished(self, summary: str):
        self.btn_sync.setEnabled(True)
        self.btn_browse_a.setEnabled(True)
        self.btn_browse_b.setEnabled(True)
        self.statusbar.showMessage(summary)
        if summary.startswith(("出错:", "部分完成 —")):
            self._sync_command_args = None
            self._successful_sync_dirs = None
            self.btn_create_service.setEnabled(False)
            if summary.startswith("部分完成 —"):
                QMessageBox.warning(self, "同步部分完成", summary)
            else:
                QMessageBox.critical(self, "同步失败", summary)
        else:
            if self._sync_command_args is not None:
                dir_a, dir_b = self._sync_command_args
                self.edit_command.setText(_build_sync_command(dir_a, dir_b))
                self._successful_sync_dirs = (dir_a, dir_b)
                self._update_service_eligibility()
            self._sync_command_args = None
            self.progress_bar.setValue(100)
            QMessageBox.information(self, "同步完成", summary)

    def _create_service(self):
        selected_dirs = (self.edit_dir_a.text(), self.edit_dir_b.text())
        if (
            self._successful_sync_dirs is None
            or selected_dirs != self._successful_sync_dirs
        ):
            self.btn_create_service.setEnabled(False)
            QMessageBox.warning(
                self, "尚未成功同步",
                "请先在当前窗口中成功完成这两个目录的交互式同步。"
            )
            return

        try:
            service_manager.require_admin()
            interval = service_manager.validate_interval(
                self.spin_service_interval.value()
            )
            local_a, local_b = _resolve_sync_paths(*selected_dirs)
            account = service_manager.current_account_name()
            service_name = service_manager.next_service_name(local_a)
            log_path = service_manager.service_log_path(service_name)
        except (ValueError, service_manager.ServiceManagerError) as exc:
            QMessageBox.critical(self, "无法创建服务", str(exc))
            return

        if service_manager.is_windows():
            mapped = [
                path for path in (local_a, local_b) if _is_mapped_drive(path)
            ]
            network_note = (
                "\n\n检测到映射网络驱动器：服务通常无法访问登录会话映射的盘符。"
                "请改用 UNC 路径（例如 \\\\server\\share\\folder），并确保该账户"
                "具有共享权限和 NTFS 权限。"
                if mapped else
                "\n\n若使用网络目录，建议配置 UNC 路径（\\\\server\\share\\folder）；"
                "服务不会继承登录会话中的映射盘符。请确保该账户具有共享权限和 NTFS 权限。"
            )
            title = "确认创建 Windows 服务"
            details = (
                "即将在 Windows 服务控制管理器中安装并立即启动服务：\n\n"
                f"服务名称：{service_name}\n"
                f"服务目录 A（解析后）：{local_a}\n"
                f"服务目录 B（解析后）：{local_b}\n"
                f"同步间隔：{interval} 分钟（服务启动后立即同步；每轮完成后等待该间隔）\n"
                f"运行账户：{account}\n"
                f"服务日志：{log_path}\n\n"
                "服务将设置为开机自动启动。"
                f"{network_note}"
            )
        else:
            title = "确认创建 Ubuntu 用户服务"
            details = (
                "即将在当前 Ubuntu 用户的 systemd 中创建并启动服务：\n\n"
                f"服务名称：{service_name}\n"
                f"服务目录 A：{local_a}\n"
                f"服务目录 B：{local_b}\n"
                f"同步间隔：{interval} 分钟（启动后立即同步，每轮完成后等待该间隔）\n"
                f"运行用户：{account}\n"
                f"服务日志：{log_path}\n\n"
                "为使服务在注销后继续运行并在重启后自动启动，需要启用该用户的 "
                "systemd linger。若尚未启用，系统将弹出管理员授权对话框，仅执行 "
                "loginctl enable-linger；FolderSync 服务仍以当前普通用户身份运行，"
                "不会获得 root 文件权限。Ubuntu 用户密码不会交给或保存到 FolderSync。"
                "\n\n如果 SMB/GVFS 共享未挂载，本轮会记录失败，并在下次周期重试。"
            )
        if QMessageBox.question(
            self, title, details,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return

        password_dialog = None
        password = ""
        if service_manager.requires_account_password():
            password_dialog = _PasswordDialog(account, self)
            if password_dialog.exec() != QDialog.DialogCode.Accepted:
                password_dialog.password_edit.clear()
                password_dialog.deleteLater()
                return
            password = password_dialog.password_edit.text()
            password_dialog.password_edit.clear()
        try:
            service_manager.create_service(
                local_a,
                local_b,
                local_a,
                local_b,
                interval,
                account,
                password,
                service_name,
            )
        except service_manager.ServiceStartError as exc:
            remove = QMessageBox.question(
                self, "服务已安装但启动失败",
                f"{exc}\n\n是否现在删除已安装的服务？选择“否”会保留服务，"
                "可在服务列表中重试启动。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if remove == QMessageBox.StandardButton.Yes:
                try:
                    service_manager.delete_service(exc.service_name)
                    QMessageBox.information(self, "服务已删除", "服务已停止并删除。")
                except service_manager.ServiceManagerError as delete_error:
                    QMessageBox.critical(self, "删除失败", str(delete_error))
            else:
                QMessageBox.warning(self, "服务未启动", str(exc))
        except service_manager.ServiceManagerError as exc:
            QMessageBox.critical(self, "服务创建失败", str(exc))
        else:
            if service_manager.is_windows():
                QMessageBox.information(
                    self, "服务已创建",
                    f"服务“{service_name}”已安装并启动，且已设置为开机自动启动。\n\n"
                    f"服务运行状态和同步结果请查看日志：\n{log_path}"
                )
            else:
                QMessageBox.information(
                    self, "服务已创建",
                    f"Ubuntu 用户服务“{service_name}”已创建、启用并启动。\n"
                    "服务会在用户注销后继续运行，并在系统重启后自动启动。\n\n"
                    f"服务日志：\n{log_path}"
                )
        finally:
            password = ""
            if password_dialog is not None:
                password_dialog.password_edit.clear()
                password_dialog.deleteLater()
            self._refresh_services()

    def _refresh_services(self):
        if not (service_manager.is_windows() or service_manager.is_ubuntu()):
            return
        self.service_table.setRowCount(0)
        self._managed_services = {}
        self.service_command_output.clear()
        self.btn_start_service.setEnabled(False)
        try:
            services = service_manager.list_managed_services()
        except service_manager.ServiceManagerError as exc:
            self.statusbar.showMessage(f"读取服务列表失败：{exc}")
            QMessageBox.critical(self, "服务列表读取失败", str(exc))
            return

        for service in services:
            self._managed_services[service.name] = service
            row = self.service_table.rowCount()
            self.service_table.insertRow(row)
            values = (
                service.name,
                service.directory_a,
                service.directory_b,
                f"{service.interval_minutes} 分钟",
                service.status,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, service.name)
                self.service_table.setItem(row, column, item)
        self._update_delete_button()
        self.statusbar.showMessage(
            f"已刷新服务列表（{len(services)} 个 FolderSync 服务）"
        )

    def _update_delete_button(self):
        selected = self.service_table.currentRow() >= 0
        supported = service_manager.is_windows() or service_manager.is_ubuntu()
        self.btn_delete_service.setEnabled(supported and selected)
        self._update_selected_service()

    def _update_selected_service(self):
        row = self.service_table.currentRow()
        if row < 0:
            self.btn_delete_service.setEnabled(False)
            self.btn_start_service.setEnabled(False)
            self.service_command_output.clear()
            return

        service_name = self.service_table.item(row, 0).data(
            Qt.ItemDataRole.UserRole
        )
        service = self._managed_services.get(service_name)
        if service is None:
            self.btn_start_service.setEnabled(False)
            self.service_command_output.clear()
            return

        self.btn_delete_service.setEnabled(
            service_manager.is_windows() or service_manager.is_ubuntu()
        )
        self.btn_start_service.setEnabled(
            (service_manager.is_windows() or service_manager.is_ubuntu())
            and service.status not in ("运行中", "启动中", "停止中")
        )
        self.service_command_output.setPlainText(
            _build_service_command_text(service)
        )

    def _start_selected_service(self):
        row = self.service_table.currentRow()
        if row < 0:
            return

        service_name = self.service_table.item(row, 0).data(
            Qt.ItemDataRole.UserRole
        )
        service = self._managed_services.get(service_name)
        if service is None:
            QMessageBox.warning(self, "服务不可用", "请刷新服务列表后重试。")
            return

        try:
            service_manager.require_admin()
        except service_manager.ServiceManagerError as exc:
            QMessageBox.critical(self, "无法启动服务", str(exc))
            return

        password_dialog = None
        password = ""
        if service_manager.requires_account_password():
            password_dialog = _PasswordDialog(
                service.account,
                self,
                title="更新服务登录密码",
                submit_text="更新密码并启动",
            )
            if password_dialog.exec() != QDialog.DialogCode.Accepted:
                password_dialog.password_edit.clear()
                password_dialog.deleteLater()
                return

            password = password_dialog.password_edit.text()
            password_dialog.password_edit.clear()
        try:
            started = service_manager.start_managed_service(
                service_name, password
            )
        except service_manager.ServiceManagerError as exc:
            QMessageBox.critical(self, "服务启动失败", str(exc))
        else:
            if started:
                QMessageBox.information(
                    self, "服务已启动",
                    (
                        f"服务“{service_name}”已更新登录密码并启动。"
                        if service_manager.is_windows()
                        else f"服务“{service_name}”已启动。"
                    ) + "请查看服务日志确认同步结果。"
                )
            else:
                QMessageBox.information(
                    self, "服务正在运行",
                    f"服务“{service_name}”已经运行，无需再次启动。"
                )
        finally:
            password = ""
            if password_dialog is not None:
                password_dialog.password_edit.clear()
                password_dialog.deleteLater()
            self._refresh_services()

    def _copy_service_command(self):
        row = self.service_table.currentRow()
        if row < 0:
            self.statusbar.showMessage("请先在服务列表中选择一个服务")
            return

        service_name = self.service_table.item(row, 0).data(
            Qt.ItemDataRole.UserRole
        )
        command = _build_service_start_command(service_name)
        clipboard = QApplication.clipboard()
        if clipboard is None:
            QMessageBox.warning(self, "复制失败", "当前无法访问系统剪贴板。")
            return

        clipboard.setText(command)
        self.statusbar.showMessage("服务启动命令已复制")

    def _delete_selected_service(self):
        row = self.service_table.currentRow()
        if row < 0:
            return
        service_name = self.service_table.item(row, 0).data(
            Qt.ItemDataRole.UserRole
        )
        directory_a = self.service_table.item(row, 1).text()
        directory_b = self.service_table.item(row, 2).text()
        if service_manager.is_windows():
            delete_details = (
                f"将停止（如正在运行）并从 SCM 删除以下服务：\n\n"
                f"服务名称：{service_name}\n目录 A：{directory_a}\n目录 B：{directory_b}\n\n"
                "这会删除服务注册，不只是停止服务。SCM 可能会延迟完成删除。"
            )
        else:
            delete_details = (
                f"将停止并删除以下当前用户服务及其配置：\n\n"
                f"服务名称：{service_name}\n目录 A：{directory_a}\n目录 B：{directory_b}\n\n"
                "服务日志会保留。此操作不会更改系统 linger 设置。"
            )
        if QMessageBox.question(
            self, "确认删除服务",
            delete_details,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            service_manager.require_admin()
            service_manager.delete_service(service_name)
        except service_manager.ServiceManagerError as exc:
            QMessageBox.critical(self, "删除服务失败", str(exc))
        else:
            QMessageBox.information(
                self, "删除请求已提交",
                f"服务“{service_name}”已停止并删除。"
            )
        self._refresh_services()

    def _copy_command(self):
        command = self.edit_command.text()
        if not command:
            self.statusbar.showMessage("同步成功后才会生成可复制的命令")
            return

        clipboard = QApplication.clipboard()
        if clipboard is None:
            QMessageBox.warning(self, "复制失败", "当前无法访问系统剪贴板。")
            return

        clipboard.setText(command)
        self.statusbar.showMessage("命令已复制，可粘贴到 Terminal 执行")


def run_command_line(dir_a: str, dir_b: str) -> int:
    """从命令行同步两个本地路径或已挂载的 SMB URL。"""
    try:
        local_a, local_b = _resolve_sync_paths(dir_a, dir_b)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    app = QCoreApplication([sys.argv[0]])
    worker = SyncWorker(local_a, local_b)
    worker.log.connect(print)

    exit_code = 1

    def on_finished(summary: str):
        nonlocal exit_code
        print(summary)
        exit_code = 0 if summary.startswith("完成 —") else 1
        app.quit()

    worker.finished_signal.connect(on_finished)
    worker.start()
    app.exec()
    worker.wait()
    return exit_code


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--run-service":
        sys.exit(service_manager.run_service(sys.argv[2], _run_sync_once))
    if len(sys.argv) == 3 and sys.argv[1] == "--run-user-service":
        sys.exit(service_manager.run_service(sys.argv[2], _run_sync_once))
    if len(sys.argv) == 3:
        sys.exit(run_command_line(sys.argv[1], sys.argv[2]))
    if len(sys.argv) != 1:
        print(
            f"用法: {Path(sys.argv[0]).name} <目录A> <目录B>",
            file=sys.stderr
        )
        sys.exit(2)

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
