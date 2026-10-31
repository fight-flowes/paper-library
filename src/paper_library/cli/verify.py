"""Reproducible local/CI checks without calling model services."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main(argv=None):
    parser=argparse.ArgumentParser(prog='scripts/verify',description='完整 Python 检查；可追加隔离浏览器或当前阅读器验收')
    parser.add_argument('--browser',action='store_true',help='追加临时论文库的导入浏览器测试')
    parser.add_argument('--live',action='store_true',help='追加当前运行阅读器的布局、翻译、导读与工作区验收')
    args=parser.parse_args(argv)
    root=Path(__file__).resolve().parents[3]
    commands=[[sys.executable,'-m','pytest','-q']]
    if args.browser:commands.extend(['node',f'tests/browser/{name}.cjs'] for name in ('management','settings'))
    if args.live:commands.extend(['node',f'tests/browser/{name}.cjs'] for name in ('layout','workspace','translation','guide','frontend-tools'))
    for command in commands:
        print('检查：'+' '.join(command),flush=True)
        result=subprocess.run(command,cwd=root,env={**os.environ,"PAPER_TEST_PYTHON":sys.executable})
        if result.returncode:return result.returncode
    return 0


if __name__=='__main__':raise SystemExit(main())
