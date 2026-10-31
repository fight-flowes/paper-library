"""Inspect and maintain normalized paper libraries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from paper_library.catalog import LibraryError, LibraryRepository, PaperPaths
from paper_library.config import default_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts/library", description="管理本地论文库")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="列出论文库")
    commands.add_parser("check", help="校验目录、论文包与全局 ID")
    show = commands.add_parser("show", help="显示一篇论文的信息")
    show.add_argument("--library", required=True)
    show.add_argument("--paper", required=True)
    ingest = commands.add_parser("ingest", help="用 MinerU 导入 PDF")
    ingest.add_argument("--library", required=True)
    ingest.add_argument("pdf", type=Path)
    ingest.add_argument("--subject", default="")
    ingest.add_argument("--lang", choices=("ch", "en"), default="en")
    ingest.add_argument("--force", action="store_true")
    ingest.add_argument("--dry-run", action="store_true")
    ingest.add_argument("--no-enrich", action="store_true")
    refresh = commands.add_parser("refresh-metadata", help="刷新一篇论文的书目信息")
    refresh.add_argument("--library", required=True)
    refresh.add_argument("--paper", required=True)
    repair = commands.add_parser("repair-paragraphs", help="用当前模型检查并修复单篇论文断段")
    repair.add_argument("--library", required=True)
    repair.add_argument("--paper", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    command_parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        command_parser.print_help()
        return 0
    args = command_parser.parse_args(arguments)
    settings = default_settings()
    repository = LibraryRepository(settings.paths.libraries)
    try:
        if args.command == "repair-paragraphs":
            from paper_library.ingest.paragraphs import repair_file
            from paper_library.ingest.service import _lock
            snapshot = repository.load(args.library)
            paper = snapshot.find(args.paper)
            if paper is None:
                raise ValueError("论文不存在")
            with _lock(snapshot.descriptor.root):
                report = repair_file(PaperPaths.for_id(snapshot.descriptor.root, paper.id).markdown, settings=settings)
            print(f"段落检查完成：候选 {len(report['decisions'])}，合并 {report['merged']}")
            return 0
        if args.command == "list":
            for library_id, descriptor in repository.discover().items():
                count = len(repository.load(library_id).papers)
                print(f"{library_id}\t{count}\t{descriptor.config.name}")
            return 0
        if args.command == "check":
            problems = repository.validate_all()
            if problems:
                for problem in problems:
                    print(f"ERROR\t{problem}")
                return 1
            libraries = repository.discover()
            papers = sum(len(repository.load(key).papers) for key in libraries)
            print(f"校验通过：{len(libraries)} 个论文库，{papers} 篇论文。")
            return 0
        if args.command == "show":
            snapshot = repository.load(args.library)
            paper = snapshot.find(args.paper)
            if paper is None:
                raise ValueError(f"未找到论文：{args.library}/{args.paper}")
            paths = PaperPaths.for_id(snapshot.descriptor.root, paper.id)
            print(json.dumps({
                "library": args.library,
                "paper": {field: getattr(paper, field) for field in (
                    "id", "year", "venue", "short_name", "subject", "title",
                    "sha256", "parsed_at", "publication_date", "aliases",
                )},
                "paths": {"pdf": str(paths.pdf), "markdown": str(paths.markdown), "metadata": str(paths.metadata)},
            }, ensure_ascii=False, indent=2))
            return 0
        from paper_library.ingest.service import ingest_pdf, refresh_metadata
        if args.command == "ingest":
            result = ingest_pdf(repository, args.library, args.pdf, subject=args.subject, language=args.lang, force=args.force, dry_run=args.dry_run, enrich=not args.no_enrich)
            if args.dry_run:
                label = "计划导入"
            else:
                labels = {
                    "created": "已导入",
                    "updated": "已更新",
                    "unchanged": "未变更",
                }
                label = labels[result.outcome.value]
            print(f"{label}：{result.library_id}/{result.paper_id}")
            return 0
        result = refresh_metadata(repository, args.library, args.paper, settings=settings)
        print(result)
        return 0
    except (LibraryError, OSError, ValueError) as error:
        print(f"错误：{error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
