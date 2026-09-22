"""Offline translation commands."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from threading import Event

from paper_library.catalog import LibraryError, LibraryRepository
from paper_library.config import default_settings
from paper_library.translation.batch import load_tasks, run_batch
from paper_library.translation.progress import TranslationProgress
from paper_library.translation.service import TranslationConfig, TranslationError, TranslationService


def bounded(minimum: int, maximum: int):
    def parse(value: str) -> int:
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"必须在 {minimum} 到 {maximum} 之间")
        return number
    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/translate",
        description="使用 .env 中配置的本地模型离线生成段落译文；网页只读取缓存。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  scripts/translate check
  scripts/translate status --all
  scripts/translate plan --library mechanism-interpretability --paper mi-p0001
  scripts/translate run --library mechanism-interpretability --paper mi-p0001
  scripts/translate retry-warnings --library whitespace""",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="检查模型配置")
    command_help = {
        "status": "统计已缓存、待处理和带阅读提示的段落",
        "plan": "生成执行计划和报告，但不调用模型",
        "run": "翻译缺失段落并立即保存",
        "retry-warnings": "重新翻译缓存中带阅读提示的段落",
    }
    for name, help_text in command_help.items():
        command = commands.add_parser(name, help=help_text)
        scope = command.add_mutually_exclusive_group(required=True)
        scope.add_argument("--all", action="store_true")
        scope.add_argument("--library")
        command.add_argument("--paper")
        command.add_argument("--concurrency", type=bounded(1, 32))
        command.add_argument("--retries", type=bounded(0, 5), default=2)
        command.add_argument("--report", type=Path)
    return parser


def _tasks(repository: LibraryRepository, args) -> list:
    if args.paper and not args.library:
        raise ValueError("--paper 必须与 --library 一起使用")
    ids = list(repository.discover()) if args.all else [args.library]
    return [task for library_id in ids for task in load_tasks(repository, library_id, args.paper)]


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    command_parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        command_parser.print_help()
        return 0
    args = command_parser.parse_args(arguments)
    settings = default_settings()
    try:
        configured = settings.translation(required=args.command in {"run", "retry-warnings"})
        config = TranslationConfig.from_settings(configured)
        if args.command == "check":
            state = "可用" if config.configured else "未配置"
            print(f"{state}：profile={config.profile or '-'} model={config.model or '-'} concurrency={config.concurrency}")
            return 0 if config.configured else 1
        repository = LibraryRepository(settings.paths.libraries)
        tasks = _tasks(repository, args)
        concurrency = args.concurrency or config.concurrency
        service = TranslationService(config, settings.paths.cache, max_concurrency=concurrency)
        progress = TranslationProgress()
        stop = Event()
        previous = signal.signal(signal.SIGINT, lambda *_: stop.set())
        dry_run = args.command in {"status", "plan"}
        try:
            with ExitStack() as stack:
                if not dry_run:
                    service.cache_path.parent.mkdir(parents=True, exist_ok=True)
                    lock = stack.enter_context(service.cache_path.with_suffix(".batch.lock").open("a"))
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as error:
                        raise ValueError("已有翻译任务正在运行") from error
                report = run_batch(
                    tasks, service, concurrency=concurrency, retries=args.retries,
                    dry_run=dry_run, stop_event=stop, progress=progress.message,
                    on_update=progress.update,
                    retry_warnings=args.command == "retry-warnings",
                )
        finally:
            progress.close()
            signal.signal(signal.SIGINT, previous)
        now = datetime.now(timezone.utc)
        report.update(finished_at=now.isoformat(), dry_run=dry_run)
        report["warning_segments"] = sum(bool(item.get("warnings")) for item in report["results"])
        if args.command == "status":
            print(
                f"总计 {report['total']}；已保存 {report['cached']}；"
                f"待处理 {report['pending']}；阅读提示 {report['warning_segments']}"
            )
            return 0
        report_path = args.report or settings.paths.cache.parent / "reports" / (now.strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        _write_report(report_path, report)
        print(f"完成：新增 {report['translated']}，已保存 {report['cached']}，失败 {report['failed']}，待处理 {report['pending']}")
        print(f"报告：{report_path}")
        if report["interrupted"]:
            return 130
        return 1 if report["failed"] else 0
    except (LibraryError, TranslationError, OSError, ValueError) as error:
        print(f"错误：{error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
