"""
Folder Sync - 目录同步工具
比较两个目录（含子目录）的文件，按修改日期更新。
特殊规则：文件或目录名以 DEL 开头的，会删除对面目录中对应的文件或目录，然后删除自己。
"""

import errno
import sys
import os
import shlex
import shutil
import subprocess
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlparse
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QFileDialog, QMessageBox
)
from PyQt6.QtCore import QCoreApplication, QThread, pyqtSignal

# 导入 Qt Designer 生成的 UI 类
from ui_main_window import Ui_MainWindow


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

    def get_all_files(self, root: Path) -> dict:
        """
        递归扫描目录，返回 {相对路径: 绝对路径} 的字典。
        相对路径统一用正斜杠，保证跨平台比较一致。
        """
        files = {}
        for path in root.rglob('*'):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                files[rel] = path
        return files

    def get_all_dirs(self, root: Path) -> dict:
        """
        递归扫描目录，返回 {相对路径: 绝对路径} 的字典（仅目录）。
        """
        dirs = {}
        for path in root.rglob('*'):
            if path.is_dir():
                rel = path.relative_to(root).as_posix()
                dirs[rel] = path
        return dirs

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

    def _process_del_dirs(self, root: Path, opposite_root: Path) -> int:
        """
        处理 root 下所有以 DEL 开头的目录：
        1. 在 opposite_root 中找到对应目录并递归删除
        2. 删除 DEL 目录自身
        返回删除数量。
        """
        deleted = 0
        dirs = self.get_all_dirs(root)
        # 按路径长度降序排列，先处理深层目录
        for rel in sorted(dirs.keys(), key=len, reverse=True):
            name = Path(rel).name
            if not name.upper().startswith("DEL"):
                continue

            target_name = name[3:]
            if not target_name:
                self.log.emit(f"  [跳过] DEL 目录名缺少目标名称: {rel}")
                continue
            target_rel = str(Path(rel).parent / target_name)
            target_path = opposite_root / target_rel
            del_path = dirs[rel]

            if target_path.exists() and target_path.is_dir():
                shutil.rmtree(str(target_path))
                self.log.emit(f"  [删除目录] {target_rel}")
                deleted += 1

            if del_path.exists():
                shutil.rmtree(str(del_path))
                self.log.emit(f"  [删除目录] DEL 指令目录 {rel}")

        return deleted

    def _process_del_files(
        self, root_a: Path, root_b: Path
    ) -> tuple[int, set[str]]:
        """处理两边以 DEL 开头的文件，返回删除数和本轮不再同步的目标路径。"""
        files_a = self.get_all_files(root_a)
        files_b = self.get_all_files(root_b)
        deleted = 0
        skipped_paths = set()

        for rel in sorted(set(files_a) | set(files_b)):
            name = Path(rel).name
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

            target_rel = str(Path(rel).parent / target_name)
            skipped_paths.add(target_rel)
            source_root, opposite_root = (
                (root_a, root_b) if in_a else (root_b, root_a)
            )
            del_path = source_root / rel
            if not del_path.is_file():
                continue

            target_path = opposite_root / target_rel
            if target_path.is_file():
                target_path.unlink()
                self.log.emit(f"  [删除文件] {target_rel}")
                deleted += 1
                self._remove_empty_parents(target_path.parent, opposite_root)

            if del_path.exists():
                del_path.unlink()
                self.log.emit(f"  [删除文件] DEL 指令文件 {rel}")
                self._remove_empty_parents(del_path.parent, source_root)

        return deleted, skipped_paths

    def run(self):
        try:
            self.log.emit(f"扫描目录 A: {self.dir_a}")
            self.log.emit(f"扫描目录 B: {self.dir_b}")

            copied = 0
            deleted = 0

            # ── 第一阶段：处理 DEL 目录和文件 ──
            self.log.emit("\n第一阶段：处理 DEL 删除指令...\n")

            del_a = self._process_del_dirs(self.dir_a, self.dir_b)
            del_b = self._process_del_dirs(self.dir_b, self.dir_a)
            del_files, del_file_targets = self._process_del_files(
                self.dir_a, self.dir_b
            )
            deleted += del_a + del_b + del_files

            if del_a + del_b + del_files == 0:
                self.log.emit("  无 DEL 删除指令")

            # ── 第二阶段：扫描文件并同步 ──
            self.log.emit("\n第二阶段：扫描文件并同步...\n")

            files_a = self.get_all_files(self.dir_a)
            self.log.emit(f"  目录 A 找到 {len(files_a)} 个文件")
            files_b = self.get_all_files(self.dir_b)
            self.log.emit(f"  目录 B 找到 {len(files_b)} 个文件")

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

                in_a = rel in files_a
                in_b = rel in files_b

                # ── 正常文件同步逻辑 ──
                if in_a and not in_b:
                    src = files_a[rel]
                    dst = self.dir_b / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    self._copy_file(src, dst, rel)
                    self.log.emit(f"  [A→B] {rel}")
                    copied += 1

                elif in_b and not in_a:
                    src = files_b[rel]
                    dst = self.dir_a / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    self._copy_file(src, dst, rel)
                    self.log.emit(f"  [B→A] {rel}")
                    copied += 1

                else:
                    mtime_a = files_a[rel].stat().st_mtime
                    mtime_b = files_b[rel].stat().st_mtime

                    if abs(mtime_a - mtime_b) < 1.0:
                        continue

                    if mtime_a > mtime_b:
                        dst = self.dir_b / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        self._copy_file(files_a[rel], dst, rel)
                        self.log.emit(f"  [A→B 更新] {rel}")
                        copied += 1
                    else:
                        dst = self.dir_a / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        self._copy_file(files_b[rel], dst, rel)
                        self.log.emit(f"  [B→A 更新] {rel}")
                        copied += 1

            summary = (
                f"\n{'='*40}\n"
                f"同步完成！\n"
                f"  复制: {copied} 个文件\n"
                f"  删除: {deleted} 个文件/目录\n"
                f"{'='*40}"
            )
            self.log.emit(summary)
            self.finished_signal.emit(
                f"完成 — 复制 {copied} 个，删除 {deleted} 个"
            )

        except Exception as e:
            self.log.emit(f"\n[错误] {e}")
            self.finished_signal.emit(f"出错: {e}")

    def _remove_empty_parents(self, dir_path: Path, stop_at: Path):
        """向上递归删除空目录，直到 stop_at"""
        try:
            while dir_path != stop_at and dir_path.is_dir():
                if any(dir_path.iterdir()):
                    break
                dir_path.rmdir()
                self.log.emit(f"  [清理] 空目录 {dir_path.relative_to(stop_at)}")
                dir_path = dir_path.parent
        except OSError:
            pass


# ──────────────────────────────────────────────
# 主窗口
# ──────────────────────────────────────────────

class MainWindow(QMainWindow, Ui_MainWindow):
    def __init__(self):
        super().__init__()
        self.setupUi(self)
        self.worker = None
        self._sync_command_args = None

        # 连接信号
        self.btn_browse_a.clicked.connect(self._browse_dir_a)
        self.btn_browse_b.clicked.connect(self._browse_dir_b)
        self.btn_sync.clicked.connect(self._start_sync)
        self.btn_copy_command.clicked.connect(self._copy_command)

        self.statusbar.showMessage("就绪 — 请选择两个目录")

    def _browse_dir_a(self):
        dir_path = pick_folder("选择目录 A", self.edit_dir_a.text())
        if dir_path:
            self.edit_dir_a.setText(dir_path)
            self.statusbar.showMessage(f"目录 A: {dir_path}")

    def _browse_dir_b(self):
        dir_path = pick_folder("选择目录 B", self.edit_dir_b.text())
        if dir_path:
            self.edit_dir_b.setText(dir_path)
            self.statusbar.showMessage(f"目录 B: {dir_path}")

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
        if summary.startswith("出错:"):
            self._sync_command_args = None
            QMessageBox.critical(self, "同步失败", summary)
        else:
            if self._sync_command_args is not None:
                dir_a, dir_b = self._sync_command_args
                self.edit_command.setText(_build_sync_command(dir_a, dir_b))
            self._sync_command_args = None
            self.progress_bar.setValue(100)
            QMessageBox.information(self, "同步完成", summary)

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
        exit_code = 1 if summary.startswith("出错:") else 0
        app.quit()

    worker.finished_signal.connect(on_finished)
    worker.start()
    app.exec()
    worker.wait()
    return exit_code


if __name__ == "__main__":
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
