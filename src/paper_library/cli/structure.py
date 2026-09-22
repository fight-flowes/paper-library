"""Precompute optional document structure annotations."""
import argparse
import sys

from paper_library.catalog import LibraryError, LibraryRepository, PaperPaths
from paper_library.config import default_settings
from paper_library.structure.document import load_structure
from paper_library.structure.service import StructureService


def main(argv=None):
    parser = argparse.ArgumentParser(prog='scripts/structure', description='使用 .env 当前模型校对文本结构，保留原文；网页读取已保存标注')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('run','status'):
        command = commands.add_parser(name, help='生成结构标注' if name == 'run' else '查看结构标注状态')
        scope = command.add_mutually_exclusive_group(required=True)
        scope.add_argument('--library')
        scope.add_argument('--all', action='store_true')
        command.add_argument('--paper')
        if name == 'run':
            command.add_argument('--force', action='store_true', help='重新请求模型，不复用已有标注')
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        parser.print_help(); return 0
    args = parser.parse_args(argv)
    if args.paper and not args.library:
        parser.error('--paper 必须与 --library 一起使用')
    try:
        settings = default_settings()
        repository = LibraryRepository(settings.paths.libraries)
        service = StructureService(settings) if args.command == 'run' else None
        failed = []
        for library in repository.discover() if args.all else [args.library]:
            snapshot = repository.load(library)
            papers = snapshot.papers
            if args.paper:
                paper = snapshot.find(args.paper)
                if paper is None:
                    raise ValueError('未找到指定论文')
                papers = [paper]
            for paper in papers:
                paths = PaperPaths.for_id(snapshot.descriptor.root, paper.id)
                print(f'{library}/{paper.id}', flush=True)
                try:
                    if service:
                        data = service.generate(paths.markdown, force=args.force,
                            progress=lambda d,t:print(f'结构校对 {d}/{t} 块', flush=True))
                        uncertain = sum(r['role'] == 'unknown' for r in data['annotations'])
                        print(f'已保存 {len(data["annotations"])} 块，待确认 {uncertain} 块；{paths.root / "document-structure.json"}', flush=True)
                    else:
                        data = load_structure(paths.root, paths.markdown.read_text(encoding='utf-8'))
                        print('已生成，原文匹配' if data else '未生成、已过期或文件无效')
                except (ValueError, OSError) as exc:
                    failed.append(f'{library}/{paper.id}')
                    print(f'错误：{exc}', file=sys.stderr, flush=True)
        return 1 if failed else 0
    except (LibraryError, ValueError, OSError) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
