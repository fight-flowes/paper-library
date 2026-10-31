"""Single-line structure progress; keep redirected logs free of redraws."""
import sys


class StructureProgress:
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.interactive = self.stream.isatty()
        self.visible = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def update(self, done, total):
        if not self.interactive:
            return
        total = max(0, total)
        done = max(0, min(done, total))
        percent = done * 100 // total if total else 100
        filled = done * 16 // total if total else 16
        bar = '#' * filled + '-' * (16 - filled)
        self.stream.write(f'\r\033[2K结构校对 [{bar}] {percent}% {done}/{total} 项')
        self.stream.flush()
        self.visible = True

    def close(self):
        if self.visible:
            self.stream.write('\n')
            self.stream.flush()
            self.visible = False
