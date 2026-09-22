"""Endpoint-wide flock slots shared by CLI, HTTP workers and model tasks."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile
import time

import httpx


@contextmanager
def model_client(config, *, transport=None, cancelled=lambda: False):
    directory = config.budget_directory or Path(tempfile.gettempdir()) / f'paper-library-llm-{os.getuid()}'
    endpoint = hashlib.sha256(config.base_url.rstrip('/').encode()).hexdigest()
    root = Path(directory) / endpoint
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    acquired = None
    deadline = time.monotonic() + config.timeout
    try:
        while acquired is None:
            if cancelled():
                raise ValueError('模型任务已取消')
            for index in range(config.budget):
                stream = (root / f'{index}.lock').open('a')
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    stream.close()
                else:
                    acquired = stream
                    break
            if acquired is None:
                if time.monotonic() >= deadline:
                    raise httpx.PoolTimeout('等待共享模型并发额度超时')
                time.sleep(0.05)
        with httpx.Client(timeout=httpx.Timeout(config.timeout, connect=10), trust_env=False, transport=transport) as client:
            yield client
    finally:
        if acquired is not None:
            fcntl.flock(acquired, fcntl.LOCK_UN)
            acquired.close()


def provenance(config, *, max_tokens=None):
    return {'model': config.model, 'profile': config.profile,
            'temperature': config.temperature, 'max_tokens': max_tokens or config.max_tokens,
            'request_style': config.request_style}
