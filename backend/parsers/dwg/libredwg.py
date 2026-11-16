"""LibreDWG 独立命令行调用；日志落 cache，超时回收整个工作进程组。"""
from __future__ import annotations

import math
import re
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class LibreDwgTools:
    dwg2dxf: str
    dwgread: str


@dataclass(frozen=True)
class DwgOptions:
    timeout_sec: float = 120
    memory_mb: int = 1536
    worker_timeout_sec: float = 1200

    def __post_init__(self):
        if (not math.isfinite(self.timeout_sec) or self.timeout_sec <= 0
                or not math.isfinite(self.worker_timeout_sec) or self.worker_timeout_sec <= 0
                or self.memory_mb < 64):
            raise ValueError("超时必须为有限正数，内存上限至少为 64 MB")

    @classmethod
    def from_env(cls):
        timeout = float(os.environ.get("DRAWCHECK_DWG_TIMEOUT_SEC", "120"))
        return cls(timeout, int(os.environ.get("DRAWCHECK_DWG_MEMORY_MB", "1536")),
                   float(os.environ.get("DRAWCHECK_DWG_WORKER_TIMEOUT_SEC", str(max(1200, 2 * timeout + 120)))))


def discover_tools() -> LibreDwgTools | None:
    """显式目录优先且不回退，方便禁用或固定已验证的 0.13.3 组件。"""
    directory = os.environ.get("DRAWCHECK_LIBREDWG_BIN")
    search = directory if directory is not None else os.environ.get("PATH", "")
    names = [shutil.which(name, path=search) for name in ("dwg2dxf", "dwgread")]
    if not all(names):
        return None
    return LibreDwgTools(*(str(Path(name).resolve()) for name in names))


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    reason: str = ""
    stderr_tail: str = ""


def _stop(proc: subprocess.Popen, group: bool):
    """先 terminate，再 kill；即使组长已退出，也清理可能留下的后代。"""
    if os.name == "nt":
        # Windows 无 POSIX 进程组信号，使用系统命令尽力清理整个进程树。
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        return

    def send(sig):
        try:
            if group and os.name == "posix":
                os.killpg(proc.pid, sig)
            elif proc.poll() is None:
                proc.terminate() if sig == signal.SIGTERM else proc.kill()
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        proc.wait(timeout=0.3)
    except subprocess.TimeoutExpired:
        pass
    send(getattr(signal, "SIGKILL", signal.SIGTERM))
    proc.wait()


def run_command(command: list[str], work_dir: Path, label: str, options: DwgOptions,
                *, timeout: float | None = None, own_group: bool = True) -> CommandResult:
    """不经 shell；限制器也在子进程执行。所有 stdout/stderr 均只写 cache。"""
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parents[3])
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    env["MPLCONFIGDIR"] = str(work_dir / "matplotlib")
    env["XDG_CACHE_HOME"] = str(work_dir / "runtime-cache")
    env["TMPDIR"] = str(work_dir)
    env["TEMP"] = str(work_dir)
    env["TMP"] = str(work_dir)
    env["MPLBACKEND"] = "Agg"
    # DXF text is shaped by ezdxf into vector paths before Matplotlib receives it.
    # Matplotlib only needs its bundled backend fonts. In particular, its macOS
    # system_profiler font inventory can hang for minutes in a sandbox. This does
    # not disable ezdxf's independent installed-font lookup or glyph checks.
    env["MPL_IGNORE_SYSTEM_FONTS"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # 防止 BLAS 默认多线程消耗大量虚拟地址空间，影响 Linux 限额。
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        env[name] = "1"
    wrapped = [sys.executable, "-m", "backend.parsers.dwg._limit_exec",
               str(options.memory_mb), *command]
    err_path = work_dir / f"{label}.stderr.log"
    reason = ""
    returncode = None
    with (work_dir / f"{label}.stdout.log").open("wb") as out, err_path.open("wb") as err:
        try:
            proc = subprocess.Popen(wrapped, cwd=work_dir, env=env, stdout=out, stderr=err,
                                    start_new_session=own_group and os.name == "posix")
            try:
                returncode = proc.wait(timeout=timeout if timeout is not None else options.timeout_sec)
            except subprocess.TimeoutExpired:
                _stop(proc, own_group)
                returncode = proc.returncode
                reason = f"{label} 超时，已终止子进程"
            except BaseException:
                _stop(proc, own_group)
                raise
        except OSError as exc:
            reason = f"{label} 无法启动：{exc}"
    with err_path.open("rb") as stream:
        stream.seek(max(0, err_path.stat().st_size - 2000))
        tail = stream.read().decode("utf-8", errors="replace")
    if not reason and returncode != 0:
        # M4：被信号终止（LibreDWG 超地址空间上限时常为段错误）或 Python MemoryError 时写明内存上限。
        # N2：面向用户的 reason 不附 stderr 原文与绝对路径；ERROR 码保留，日志仍在 cache。
        limit = (f"；Linux 已施加 {options.memory_mb} MB 地址空间上限（DRAWCHECK_DWG_MEMORY_MB），"
                 f"若为内存不足可调大上限后重试") if sys.platform.startswith("linux") else ""
        codes = re.findall(r"(?:READ\s+)?ERROR\s+(0x[0-9A-Fa-f]+|\d+)", tail, flags=re.I)
        if returncode is not None and returncode < 0:
            reason = f"{label} 被信号 {-returncode} 终止，崩溃或内存不足{limit}"
        elif "MemoryError" in tail:
            reason = f"{label} 解析内存超过上限 {options.memory_mb} MB（退出码 {returncode}）"
        elif codes:
            reason = f"{label} 读取失败（ERROR {codes[-1]}，文件结构损坏或版本不支持）"
        else:
            reason = f"{label} 转换失败（退出码 {returncode}）"
    return CommandResult(returncode, reason, tail)


def run_dwg2dxf(tools: LibreDwgTools, source: Path, target: Path, options: DwgOptions):
    return run_command([tools.dwg2dxf, "-y", "-o", str(target), str(source)],
                       target.parent, "dwg2dxf", options, own_group=False)


def run_dwgread_json(tools: LibreDwgTools, source: Path, target: Path, options: DwgOptions):
    return run_command([tools.dwgread, "-O", "JSON", "-o", str(target), str(source)],
                       target.parent, "dwgread", options, own_group=False)
