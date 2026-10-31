"""Standalone worker deployed ONLY on cicore01; no application dependencies."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import select
import subprocess
import sys
import tarfile
import threading
import time

ROOT = Path(__file__).resolve().parent


def watch_connection(lost):
    try:
        # A buffered stdin read can hold its lock during interpreter shutdown.
        last_contact = time.monotonic()
        while not lost.is_set():
            ready, _, _ = select.select([sys.stdin.fileno()], [], [], 5)
            if ready:
                if not os.read(sys.stdin.fileno(), 1024):
                    break
                last_contact = time.monotonic()
            elif time.monotonic() - last_contact >= 45:
                break
    except OSError:
        pass
    finally:
        lost.set()


def validate_installation():
    from importlib.metadata import version
    mineru_version = version('mineru')
    if not os.access(Path(sys.executable).parent / 'mineru', os.X_OK):
        raise ValueError('cicore01 MinerU is not installed')
    for row in json.loads((ROOT / 'models-manifest.json').read_text()):
        path = ROOT / 'models' / row['path']
        if not path.is_file() or path.stat().st_size != row['size']:
            raise ValueError('cicore01 model missing or incomplete: ' + row['path'])
    return mineru_version


def process_identity(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        fields = text[text.rfind(')') + 2:].split()
        return int(fields[1]), fields[19], fields[0]
    except (OSError, ValueError, IndexError):
        return None


def owned_tree(pid, born):
    identity = process_identity(pid)
    if not identity or identity[1] != born:
        return {}
    snapshot = {}
    for path in Path('/proc').iterdir():
        if path.name.isdigit():
            value = process_identity(int(path.name))
            if value:
                snapshot[int(path.name)] = value
    owned = {pid: born}
    changed = True
    while changed:
        changed = False
        for child, (parent, started, _state) in snapshot.items():
            if parent in owned and child not in owned:
                owned[child] = started
                changed = True
    return owned


def alive(pid, born):
    identity = process_identity(pid)
    return bool(identity and identity[1] == born and identity[2] != 'Z')


def signal_owned(pid, born, sig):
    if not alive(pid, born):
        return
    try:
        # pidfds pin the process identity even if a PID is recycled meanwhile.
        with_pidfd = hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal')
        if with_pidfd:
            fd = os.pidfd_open(pid)
            try:
                if alive(pid, born):
                    signal.pidfd_send_signal(fd, sig)
            finally:
                os.close(fd)
        elif alive(pid, born):
            os.kill(pid, sig)
    except ProcessLookupError:
        pass


def stop(process, owned, born):
    # MinerU's temporary API server starts a separate session. Track only this
    # job's descendants (with birth identities), including reparented children.
    owned.update(owned_tree(process.pid, born))
    for pid, started in owned.items():
        signal_owned(pid, started, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while any(alive(pid, started) for pid, started in owned.items()) and time.monotonic() < deadline:
        time.sleep(.05)
    for pid, started in owned.items():
        signal_owned(pid, started, signal.SIGKILL)
    process.wait(timeout=3)
    deadline = time.monotonic() + 3
    while any(alive(pid, started) for pid, started in owned.items()) and time.monotonic() < deadline:
        time.sleep(.05)
    if any(alive(pid, started) for pid, started in owned.items()):
        raise ValueError('cicore01 parser descendants did not exit')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'parse', 'cleanup', 'check'))
    parser.add_argument('job', nargs='?', default='')
    parser.add_argument('--language', choices=('en', 'ch'), default='en')
    parser.add_argument('--timeout', type=int, default=21600)
    parser.add_argument('--window', type=int, default=8)
    parser.add_argument('--batch', type=int, default=1)
    args = parser.parse_args()
    deployment = json.loads((ROOT / 'deployment.json').read_text())
    if deployment['machine_id'] != Path('/etc/machine-id').read_text().strip():
        raise ValueError('Worker is restricted to the deployed cicore01 host')
    if args.action == 'check':
        mineru_version = validate_installation()
        import torch
        if not torch.cuda.is_available():
            raise ValueError('cicore01 CUDA unavailable')
        print(json.dumps({'cuda': True, 'gpu': torch.cuda.get_device_name(0),
                          'mineru_version': mineru_version}), flush=True)
        return
    if not re.fullmatch('[0-9a-f]{32}', args.job):
        raise ValueError('Invalid job identifier')
    directory = ROOT / 'jobs' / args.job
    if args.action == 'prepare':
        validate_installation()
        directory.mkdir(parents=True, exist_ok=False)
        return
    if args.action == 'cleanup':
        # parse owns the lock until its child has exited. Never remove live data.
        if directory.exists():
            with (directory / 'job.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                shutil.rmtree(directory)
        return
    if not (directory / 'source.pdf').is_file():
        raise ValueError('Missing uploaded PDF')
    if not 1 <= args.timeout <= 86400 or not 1 <= args.window <= 64 or not 1 <= args.batch <= 16:
        raise ValueError('Invalid resource limits')
    lost = threading.Event()
    threading.Thread(target=watch_connection, args=(lost,), daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: lost.set())
    signal.signal(signal.SIGINT, lambda *_: lost.set())
    signal.signal(signal.SIGHUP, lambda *_: lost.set())
    deadline = time.monotonic() + args.timeout
    with (directory / 'job.lock').open('a') as job_lock, (ROOT / 'gpu.lock').open('a') as gpu_lock:
        fcntl.flock(job_lock, fcntl.LOCK_EX)
        while True:
            if lost.is_set() or time.monotonic() > deadline:
                raise ValueError('Remote parsing cancelled or timed out while waiting for GPU')
            try:
                fcntl.flock(gpu_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.2)
        import torch
        if not torch.cuda.is_available():
            raise ValueError('cicore01 CUDA unavailable; refusing CPU fallback')
        env = {**os.environ, 'MINERU_MODEL_SOURCE': 'local',
               'MINERU_TOOLS_CONFIG_JSON': str(ROOT / 'mineru.json'),
               'MINERU_PROCESSING_WINDOW_SIZE': str(args.window),
               'MINERU_HYBRID_BATCH_RATIO': str(args.batch),
               'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
               'TOKENIZERS_PARALLELISM': 'false'}
        print('cicore01 GPU: ' + torch.cuda.get_device_name(0), flush=True)
        output = directory / 'output'
        output.mkdir()
        from pypdf import PdfReader
        try:
            pdf = PdfReader(str(directory / 'source.pdf'))
            evidence = {'title': str((pdf.metadata or {}).get('/Title') or '')[:2000],
                        'text': pdf.pages[0].extract_text()[:16000] if pdf.pages else ''}
        except Exception:
            evidence = {'title': '', 'text': ''}
        (output / 'pdf-frontmatter.json').write_text(json.dumps(evidence), encoding='utf-8')
        from importlib.metadata import version
        (output / 'parser-provenance.json').write_text(json.dumps({
            'server': 'cicore01', 'mineru_version': version('mineru'),
            'gpu': torch.cuda.get_device_name(0), 'backend': 'hybrid-auto-engine',
        }), encoding='utf-8')
        command = [str(Path(sys.executable).parent / 'mineru'), '-p', str(directory / 'source.pdf'),
                   '-o', str(output), '-l', args.language, '-b', 'hybrid-auto-engine', '-d', 'cuda']
        process = subprocess.Popen(command, env=env, start_new_session=True)
        identity = process_identity(process.pid)
        born = identity[1] if identity else ''
        owned = {process.pid: born}
        last_scan = 0
        try:
            while process.poll() is None:
                if time.monotonic() - last_scan >= 1:
                    owned.update(owned_tree(process.pid, born))
                    last_scan = time.monotonic()
                if lost.is_set() or time.monotonic() > deadline:
                    raise ValueError('Remote parsing cancelled, disconnected or timed out')
                time.sleep(.2)
            if process.returncode:
                raise ValueError(f'cicore01 MinerU exited {process.returncode}')
            files = [p for p in output.rglob('*') if p.is_file() and not p.is_symlink()
                     and (p.suffix.lower() in {'.md', '.png', '.jpg', '.jpeg', '.gif', '.webp'}
                          or p.name in {'pdf-frontmatter.json', 'parser-provenance.json'})]
            markdowns = [p for p in files if p.suffix == '.md' and p.stat().st_size]
            if len(markdowns) != 1:
                raise ValueError('Expected exactly one nonempty Markdown file')
            with tarfile.open(directory / 'result.tar.gz', 'w:gz') as archive:
                for path in files:
                    archive.add(path, arcname=path.relative_to(output), recursive=False)
            print('cicore01 result ready', flush=True)
        finally:
            stop(process, owned, born)
            if lost.is_set():
                shutil.rmtree(directory, ignore_errors=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        raise SystemExit(1)
