"""Safe local reader process controller."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
from urllib.request import urlopen

from paper_library.catalog import LibraryRepository
from paper_library.config import default_settings
from paper_library.jobs import JobRepository, JobState, ManagementGuard


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="scripts/dev", description="管理本地论文阅读器")
    commands = result.add_subparsers(dest="command", required=True)
    command_help = {
        "up": "在后台启动并等待就绪",
        "stop": "安全停止本项目阅读器",
        "restart": "重启阅读器",
        "status": "显示进程与访问地址",
        "logs": "显示最近日志，-f 持续跟踪",
        "urls": "列出访问地址和健康检查地址",
        "run": "在前台运行阅读器",
        "doctor": "检查环境、目录、端口与翻译配置",
    }
    for name, help_text in command_help.items():
        command = commands.add_parser(name, help=help_text)
        if name == "logs":
            command.add_argument("-f", "--follow", action="store_true")
    return result


def _files(settings):
    return settings.paths.state / "reader.pid", settings.paths.state / "reader.log"


def _read_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text().strip())
        os.kill(value, 0)
        return value
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
        return None


def _ours(pid: int, root: Path) -> bool:
    try:
        cwd = Path(f"/proc/{pid}/cwd").resolve()
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return False
    return cwd == root and "paper_library.web.app" in command


def _ready(url: str, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return True
        except OSError:
            time.sleep(0.15)
    return False


def _listener_pids(port: int) -> set[int]:
    """Return Linux listener owners; an empty set also means the port is free."""
    result = subprocess.run(
        ["ss", "-ltnp", f"sport = :{port}"],
        check=False, capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError("无法检查监听端口所有者（ss 执行失败）")
    return {int(value) for value in re.findall(r"pid=(\d+)", result.stdout)}


def _user_service(settings) -> bool:
    try:
        result=subprocess.run(['systemctl','--user','show','paper-library.service','--property=WorkingDirectory','--value'],capture_output=True,text=True,timeout=3)
        return result.returncode==0 and result.stdout.strip()==str(settings.paths.root)
    except (OSError,subprocess.TimeoutExpired):return False


def _up(settings) -> int:
    pid_path, log_path = _files(settings)
    settings.paths.state.mkdir(parents=True, exist_ok=True)
    pid = _read_pid(pid_path)
    if pid:
        if not _ours(pid, settings.paths.root):
            raise RuntimeError(f"PID 文件指向非本项目进程：{pid}")
        if pid not in _listener_pids(settings.port):
            raise RuntimeError(f"项目进程 {pid} 未监听配置端口 {settings.port}")
        print(f"阅读器已运行：PID {pid}")
        return 0
    if _user_service(settings):
        active=subprocess.run(['systemctl','--user','is-active','--quiet','paper-library.service'],check=False)
        if active.returncode==0:
            print(f'阅读器已运行（systemd）：http://{settings.host}:{settings.port}/');return 0
    owners = _listener_pids(settings.port)
    if owners:
        raise RuntimeError(f"端口 {settings.port} 已被其他进程占用：{sorted(owners)}")
    if log_path.exists() and log_path.stat().st_size > 5*1024*1024:
        log_path.replace(log_path.with_suffix('.log.previous'))
    if _user_service(settings):
        result=subprocess.run(['systemctl','--user','start','paper-library.service'],capture_output=True,text=True,timeout=40)
        if result.returncode:raise RuntimeError('systemd 启动失败，请运行 journalctl --user -u paper-library')
        if not _ready(f'http://{settings.host}:{settings.port}/health/ready'):
            raise RuntimeError('systemd 服务未通过就绪检查')
        pid=subprocess.check_output(['systemctl','--user','show','paper-library.service','--property=MainPID','--value'],text=True).strip()
        pid_path.write_text(pid+'\n')
        print(f'阅读器由 systemd 管理：PID {pid}');return 0
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "paper_library.web.app"],
            cwd=settings.paths.root, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    pid_path.write_text(f"{process.pid}\n")
    url = f"http://{settings.host}:{settings.port}/health/ready"
    if not _ready(url) or process.poll() is not None or process.pid not in _listener_pids(settings.port):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        pid_path.unlink(missing_ok=True)
        tail = log_path.read_text(errors="replace")[-3000:] if log_path.exists() else ""
        raise RuntimeError("阅读器未通过就绪检查。\n" + tail)
    print(f"阅读器已启动：PID {process.pid}")
    print(f"http://{settings.host}:{settings.port}/")
    return 0


def _stop(settings) -> int:
    pid_path, _ = _files(settings)
    if _user_service(settings):
        result=subprocess.run(['systemctl','--user','is-active','--quiet','paper-library.service'],check=False)
        if result.returncode==0:
            subprocess.run(['systemctl','--user','stop','paper-library.service'],check=True,timeout=45)
            pid_path.unlink(missing_ok=True)
            print('阅读器 systemd 服务已停止。');return 0
    pid = _read_pid(pid_path)
    if not pid:
        print("阅读器未运行。")
        return 0
    if not _ours(pid, settings.paths.root):
        raise RuntimeError(f"拒绝停止非本项目进程：{pid}")
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        os.killpg(pid, signal.SIGKILL)
    pid_path.unlink(missing_ok=True)
    print("阅读器已停止。")
    return 0


def main(argv: list[str] | None = None) -> int:
    command_parser = parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        command_parser.print_help()
        return 0
    args = command_parser.parse_args(arguments)
    settings = default_settings()
    pid_path, log_path = _files(settings)
    try:
        if args.command == "up":
            return _up(settings)
        if args.command == "stop":
            return _stop(settings)
        if args.command == "restart":
            _stop(settings)
            return _up(settings)
        if args.command == "status":
            if _user_service(settings):
                active=subprocess.run(['systemctl','--user','is-active','--quiet','paper-library.service'],check=False)
                if active.returncode==0:
                    print(f'运行中（systemd）：http://{settings.host}:{settings.port}/');return 0
            pid = _read_pid(pid_path)
            if pid and _ours(pid, settings.paths.root) and pid in _listener_pids(settings.port):
                print(f"运行中：PID {pid}，http://{settings.host}:{settings.port}/")
                return 0
            print("未运行")
            return 1
        if args.command == "urls":
            print(f"首页\thttp://{settings.host}:{settings.port}/")
            print(f"就绪检查\thttp://{settings.host}:{settings.port}/health/ready")
            return 0
        if args.command == "logs":
            if _user_service(settings):log_path=settings.paths.state / "events.log"
            if not log_path.exists():
                print("尚无日志。")
                return 0
            if args.follow:
                return subprocess.call(["tail", "-f", str(log_path)])
            print("".join(log_path.read_text(errors="replace").splitlines(keepends=True)[-80:]), end="")
            return 0
        if args.command == "doctor":
            repository = LibraryRepository(settings.paths.libraries)
            problems = repository.validate_all()
            owners = _listener_pids(settings.port)
            pid = _read_pid(pid_path)
            port_free = not owners or bool(pid and pid in owners and _ours(pid, settings.paths.root))
            jobs_accessible = False
            queued_jobs = 0
            active_jobs = 0
            try:
                jobs = JobRepository(settings.jobs_database)
                counts = jobs.state_counts()
                queued_jobs = counts.get(JobState.QUEUED, 0)
                active_jobs = counts.get(JobState.RUNNING, 0)
                jobs_accessible = True
            except (OSError, ValueError, sqlite3.Error):
                pass
            try:
                management_enabled = ManagementGuard.from_state_directory(
                    settings.paths.state,
                    settings.host,
                    settings.remote_management_enabled,
                ).mutations_enabled
            except (OSError, RuntimeError):
                management_enabled = False
            print(json.dumps({
                "python": sys.executable, "libraries": len(repository.discover()),
                "catalog_problems": list(problems), "host": settings.host,
                "port": settings.port, "port_available_or_owned": port_free,
                "translation_profile": settings.translation(required=False).profile,
                "jobs_database_accessible": jobs_accessible,
                "queued_jobs": queued_jobs,
                "active_jobs": active_jobs,
                "upload_max_mb": settings.upload_max_bytes // (1024 * 1024),
                "management_mutations_enabled": management_enabled,
            }, ensure_ascii=False, indent=2))
            local_bind = settings.host in {"localhost", "127.0.0.1", "::1"}
            management_ok = management_enabled or not local_bind
            return 0 if not problems and port_free and jobs_accessible and management_ok else 1
        return subprocess.call([sys.executable, "-m", "paper_library.web.app"], cwd=settings.paths.root)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
