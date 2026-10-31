"""SSH/SCP transport only. This module never imports or executes local MinerU."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
from pathlib import Path, PurePosixPath
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from uuid import uuid4

from .runtime import parser_command


class Cancelled(ValueError):
    pass


def identity_arguments(values, *, check_worker=False):
    """Pin the logical cicore01 node by machine-id, independent of its hostname."""
    expected = values.get('PAPER_MINERU_REMOTE_MACHINE_ID', '')
    if not re.fullmatch(r'[0-9a-f]{32}', expected):
        raise ValueError('请固定 cicore01 的 PAPER_MINERU_REMOTE_MACHINE_ID；禁止本机 PDF 解析')
    local = Path('/etc/machine-id').read_text().strip()
    if expected == local:
        raise ValueError('解析节点不能是本机；禁止本机 PDF 解析')
    program = (
        "import sys;from pathlib import Path\n"
        "actual=Path('/etc/machine-id').read_text().strip()\n"
        "if actual!=sys.argv[1] or actual==sys.argv[2]: raise SystemExit('PDF parsing is restricted to the pinned cicore01 machine')\n"
    )
    arguments = [values['PAPER_MINERU_REMOTE_PYTHON'], '-c', program, expected, local]
    if check_worker:
        arguments[2] += "import runpy\nsys.argv=[sys.argv[3],'check']\nrunpy.run_path(sys.argv[0],run_name='__main__')\n"
        arguments.append(values['PAPER_MINERU_REMOTE_ROOT'].rstrip('/') + '/worker.py')
    return arguments


class Transport:
    def __init__(self, values, directory):
        self.values = values
        self.ssh = parser_command(values)
        self.host = values['PAPER_MINERU_REMOTE_HOST']
        self.user = values['PAPER_MINERU_REMOTE_USER']
        self.port = values.get('PAPER_MINERU_REMOTE_PORT', '22')
        self.target = self.user + '@' + self.host
        self.environment = os.environ.copy()
        self.options = ['-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
                        '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3',
                        '-o', 'NumberOfPasswordPrompts=1']
        sockets = Path(tempfile.gettempdir()) / f'paper-library-ssh-{os.getuid()}'
        sockets.mkdir(mode=0o700, exist_ok=True)
        if sockets.is_symlink() or sockets.stat().st_uid != os.getuid():
            raise ValueError('SSH connection directory is not owned by the current user')
        sockets.chmod(0o700)
        identity = hashlib.sha256((self.target + ':' + self.port).encode()).hexdigest()[:24]
        self.options.extend(['-o', 'ControlMaster=auto', '-o', 'ControlPersist=60',
                             '-o', 'ControlPath=' + str(sockets / identity)])
        password = values.get('PAPER_MINERU_REMOTE_PASSWORD', '')
        if password:
            askpass = Path(directory) / 'askpass'
            askpass.write_text('#!' + sys.executable + '\nimport os\nprint(os.environ["PAPER_MINERU_REMOTE_PASSWORD"])\n')
            askpass.chmod(0o700)
            self.environment.update(SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE='force',
                                    DISPLAY=':0', PAPER_MINERU_REMOTE_PASSWORD=password)
            self.options.extend(['-o', 'PubkeyAuthentication=no'])
        else:
            self.options.extend(['-o', 'BatchMode=yes'])
            key = values.get('PAPER_MINERU_REMOTE_IDENTITY_FILE', '')
            if key:
                self.options.extend(['-i', str(Path(key).expanduser())])
        self.cancelled = False
        self.deadline = time.monotonic() + float(values.get('PAPER_MINERU_TIMEOUT_SECONDS', '21600'))

    def execute(self, command, *, timeout=None, heartbeat=False):
        if self.cancelled:
            raise Cancelled('远程解析已取消')
        deadline = min(self.deadline, time.monotonic() + timeout) if timeout else self.deadline
        process = subprocess.Popen(command, env=self.environment, stdin=subprocess.PIPE)
        last_heartbeat = 0
        try:
            while process.poll() is None:
                if self.cancelled:
                    raise Cancelled('远程解析已取消')
                if time.monotonic() > deadline:
                    raise ValueError('cicore01 远程操作超时；禁止回退本机解析')
                if heartbeat and time.monotonic() - last_heartbeat >= 5:
                    try:
                        process.stdin.write(b'.')
                        process.stdin.flush()
                    except OSError:
                        if process.poll() == 0:
                            break
                        raise ValueError('cicore01 解析连接中断；禁止回退本机解析') from None
                    last_heartbeat = time.monotonic()
                time.sleep(0.1)
            if process.returncode:
                raise ValueError(f'cicore01 远程操作失败（{process.returncode}）；禁止回退本机解析')
        finally:
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def run(self, arguments, *, timeout=None, heartbeat=False):
        self.execute([self.ssh, *self.options, '-p', self.port, self.target,
                      shlex.join([str(arg) for arg in arguments])], timeout=timeout, heartbeat=heartbeat)

    def copy(self, source, destination):
        self.execute(['scp', *self.options, '-P', self.port, '--', str(source), str(destination)])


def extract_result(archive, output):
    """Accept only bounded regular Markdown/image files beneath the output root."""
    output = Path(output)
    allowed = {'.md', '.png', '.jpg', '.jpeg', '.gif', '.webp'}
    with tarfile.open(archive, 'r:gz') as stream:
        total = 0
        for member in stream:
            path = PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or '..' in path.parts
                    or '\\' in member.name or (path.suffix.lower() not in allowed
                    and member.name not in {'pdf-frontmatter.json', 'parser-provenance.json'})):
                raise ValueError('远程解析返回不安全的文件')
            total += member.size
            if total > 2 * 1024**3:
                raise ValueError('远程解析结果超过 2 GiB 限制')
            destination = output.joinpath(*path.parts)
            if not destination.resolve().is_relative_to(output.resolve()):
                raise ValueError('远程解析结果路径越界')
            destination.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(member) as source, destination.open('wb') as target:
                shutil.copyfileobj(source, target)


def parse_remote(source, output, language, values):
    parser_command(values)
    root = values['PAPER_MINERU_REMOTE_ROOT'].rstrip('/')
    python = values['PAPER_MINERU_REMOTE_PYTHON']
    job = uuid4().hex
    with tempfile.TemporaryDirectory(prefix='paper-remote-') as temporary:
        transport = Transport(values, temporary)
        previous = {}
        machine_verified = False
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: setattr(transport, 'cancelled', True))
        try:
            print('cicore01: connecting and checking remote environment', flush=True)
            transport.run(identity_arguments(values), timeout=15)
            machine_verified = True
            transport.run([python, root + '/worker.py', 'prepare', job], timeout=30)
            print('cicore01: uploading PDF', flush=True)
            transport.copy(Path(source).resolve(), transport.target + ':' + root + '/jobs/' + job + '/source.pdf')
            transport.run([python, root + '/worker.py', 'parse', job,
                           '--language', language,
                           '--timeout', values.get('PAPER_MINERU_TIMEOUT_SECONDS', '21600'),
                           '--window', values.get('PAPER_MINERU_PROCESSING_WINDOW_SIZE', '8'),
                           '--batch', values.get('PAPER_MINERU_HYBRID_BATCH_RATIO', '1')], heartbeat=True)
            print('cicore01: downloading parsed Markdown and images', flush=True)
            archive = Path(temporary) / 'result.tar.gz'
            transport.copy(transport.target + ':' + root + '/jobs/' + job + '/result.tar.gz', archive)
            extract_result(archive, output)
            print('cicore01: remote parsing completed', flush=True)
        finally:
            # A broken SSH connection also triggers the server's stdin watchdog.
            # Cleanup is a separate bounded command, so cancellation can kill a job.
            transport.cancelled = False
            transport.deadline = time.monotonic() + 15
            try:
                if machine_verified:
                    transport.run([python, root + '/worker.py', 'cleanup', job], timeout=12)
            except (ValueError, OSError):
                print('cicore01: cleanup unavailable; remote deadline/watchdog remains active', flush=True)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description='Remote-only MinerU transport')
    parser.add_argument('-p', type=Path, required=True)
    parser.add_argument('-o', type=Path, required=True)
    parser.add_argument('-l', choices=('en', 'ch'), required=True)
    args = parser.parse_args()
    try:
        parse_remote(args.p, args.o, args.l, os.environ)
    except (OSError, ValueError, tarfile.TarError) as error:
        print(str(error), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
