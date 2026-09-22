"""Maintenance commands; no implicit destructive restore."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys

from paper_library.config import default_settings
from paper_library.storage.backup import create_backup,verify_backup,restore_backup,prune_backups,mirror_backup,record_backup_status
from paper_library.storage.history import revisions,restore_revision
from paper_library.catalog import LibraryRepository,PaperPaths
from paper_library.operations.health import check


def main(argv=None):
    parser=argparse.ArgumentParser(prog='scripts/ops',description='备份、恢复预览、版本记录和运行检查')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('install-user-units',help='写入 systemd 用户服务及定时备份单元，不自动启动')
    command=commands.add_parser('check');command.add_argument('--network',action='store_true')
    command=commands.add_parser('backup');command.add_argument('--directory',type=Path);command.add_argument('--scheduled',action='store_true')
    for name in ('verify','restore'):
        command=commands.add_parser(name);command.add_argument('backup',type=Path)
        if name=='restore':command.add_argument('--target',type=Path);command.add_argument('--apply',action='store_true')
    command=commands.add_parser('history');command.add_argument('--library',required=True);command.add_argument('--paper',required=True)
    command=commands.add_parser('restore-version',help='校验并恢复单篇论文版本到新目录');command.add_argument('--library',required=True);command.add_argument('--paper',required=True);command.add_argument('--version',required=True);command.add_argument('--target',type=Path,required=True)
    args=sys.argv[1:] if argv is None else argv
    if not args:parser.print_help();return 0
    args=parser.parse_args(args);settings=default_settings()
    try:
        if args.command=='install-user-units':
            from paper_library.operations.systemd import write_units
            print('已写入用户服务单元：'+str(write_units(settings)))
            return 0
        if args.command=='check':
            report=check(settings,network=args.network);print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['reading_ready'] else 1
        if args.command=='backup':
            directory=args.directory or Path(settings.values.get('PAPER_BACKUP_DIR',str(settings.paths.root/'.backups'))).expanduser()
            if args.scheduled:
                dates=[]
                for manifest in directory.glob('*/manifest.json'):
                    try:
                        stamp=datetime.fromisoformat(json.loads(manifest.read_text())['created_at'])
                        if stamp.tzinfo is not None:dates.append((stamp,manifest.parent))
                    except (OSError,ValueError,KeyError,TypeError):continue
                if dates:
                    stamp,latest=max(dates)
                    if (datetime.now(timezone.utc)-stamp).total_seconds()<int(settings.values.get('PAPER_BACKUP_INTERVAL_HOURS','24'))*3600:
                        try:verify_backup(latest)
                        except ValueError:pass
                        else:
                            mirror=settings.values.get('PAPER_BACKUP_MIRROR_DIR')
                            if mirror:mirror_backup(latest,mirror)
                            record_backup_status(settings,status='ok',verified_at=datetime.now(timezone.utc).isoformat(),backup=str(latest))
                            print('最近备份校验通过，仍在计划间隔内');return 0
            record_backup_status(settings,status='waiting')
            path=create_backup(settings,directory,wait_seconds=int(settings.values.get('PAPER_BACKUP_WAIT_SECONDS','300')) if args.scheduled else 0)
            mirror=settings.values.get('PAPER_BACKUP_MIRROR_DIR')
            if mirror:mirror_backup(path,mirror)
            record_backup_status(settings,status='ok',verified_at=datetime.now(timezone.utc).isoformat(),backup=str(path))
            print('备份已保存并校验：'+str(path))
            if args.scheduled:prune_backups(directory,int(settings.values.get('PAPER_BACKUP_KEEP','7')))
        elif args.command in {'verify','restore'}:
            manifest=verify_backup(args.backup)
            print(f"校验通过：{len(manifest['files'])} 个文件，{sum(v['size'] for v in manifest['files'].values())} 字节")
            if args.command=='restore':
                if not args.target:raise ValueError('恢复需要 --target 指定新目录')
                if args.apply:print('已恢复到：'+str(restore_backup(args.backup,args.target)))
                else:print('预览：将恢复论文、任务、译文与阅读记录到 '+str(args.target)+'；添加 --apply 执行，不覆盖当前数据')
        elif args.command in {'history','restore-version'}:
            repo=LibraryRepository(settings.paths.libraries);snapshot=repo.load(args.library)
            if snapshot.find(args.paper) is None:raise ValueError('论文不存在')
            root=PaperPaths.for_id(snapshot.descriptor.root,args.paper).root
            if args.command=='history':print(json.dumps(revisions(root),ensure_ascii=False,indent=2))
            else:print('版本已校验并恢复到：'+str(restore_revision(root,args.version,args.target)))
        return 0
    except (OSError,ValueError) as exc:
        if args.command=='backup':record_backup_status(settings,status='error',error=str(exc))
        print('维护未完成：'+str(exc),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
