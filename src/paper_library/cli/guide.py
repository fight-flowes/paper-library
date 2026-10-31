"""Precompute evidence-linked reading guides with the configured model."""
import argparse
import sys

from paper_library.catalog import LibraryError, LibraryRepository, PaperPaths
from paper_library.config import default_settings
from paper_library.reading.service import GuideService, load_guide, reconcile_guide
from paper_library.reading.structure import build_document


def main(argv=None):
    parser = argparse.ArgumentParser(prog='scripts/guide', description='使用 .env 当前模型预生成中文论文导读，网页只展示已保存结果')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('run', 'status', 'reconcile'):
        command = commands.add_parser(name, help='生成或复用导读' if name == 'run' else '检查导读是否存在及是否过期')
        scope = command.add_mutually_exclusive_group(required=True)
        scope.add_argument('--library')
        scope.add_argument('--all', action='store_true')
        command.add_argument('--paper')
        if name == 'run':
            mode = command.add_mutually_exclusive_group()
            mode.add_argument('--force', action='store_true', help='重新生成，不复用已有章节缓存')
            mode.add_argument('--overview-only', action='store_true', help='仅重算全文概览，复用已校验的章节导读')
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        parser.print_help()
        return 0
    args = parser.parse_args(arguments)
    if args.paper and not args.library:
        parser.error('--paper 必须与 --library 一起使用')
    try:
        settings = default_settings()
        repository = LibraryRepository(settings.paths.libraries)
        service = GuideService(settings) if args.command == 'run' else None
        failures = []
        for library_id in repository.discover() if args.all else [args.library]:
            snapshot = repository.load(library_id)
            papers = snapshot.papers
            if args.paper:
                paper = snapshot.find(args.paper)
                if paper is None:
                    raise ValueError('未找到指定论文')
                papers = [paper]
            for paper in papers:
                paths = PaperPaths.for_id(snapshot.descriptor.root, paper.id)
                print(f'{library_id}/{paper.id}', flush=True)
                if service or args.command == 'reconcile':
                    try:
                        options = {'overview_only': True} if getattr(args, 'overview_only', False) else {}
                        guide = reconcile_guide(paths.root) if args.command == 'reconcile' else service.generate(paths.markdown, force=args.force, progress=lambda text: print(text, flush=True), **options)
                    except (ValueError, OSError) as exc:
                        failures.append(f'{library_id}/{paper.id}')
                        print(f'错误：{library_id}/{paper.id}：{exc}', file=sys.stderr, flush=True)
                        continue
                    print(f'完成：{len(guide["sections"])} 个章节区域；{paths.root / "reading-guide.json"}', flush=True)
                else:
                    version = build_document(paths.markdown.read_text(encoding='utf-8'))['document_version']
                    guide = load_guide(paths.root, version)
                    print('未生成' if guide is None else '原文已变化，需要重新生成' if guide['stale'] else f'已生成 · {guide.get("model", "")}')
        if failures:
            print(f'失败 {len(failures)} 篇：{", ".join(failures)}；请去掉 --force 重试，复用已完成章节。', file=sys.stderr)
            return 1
        return 0
    except (LibraryError, ValueError, OSError) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
