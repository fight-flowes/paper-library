"""Compact terminal progress; redirected output contains only durable messages."""
import sys


class TranslationProgress:
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.interactive = self.stream.isatty()
        self.visible = False

    def clear(self):
        if self.visible:
            self.stream.write('\r\033[2K')
            self.visible = False

    def message(self, text):
        self.clear()
        print(text, file=self.stream, flush=True)

    def update(self, report, error=None):
        if error:
            self.message(error)
        if not self.interactive:
            return
        total = report['total']
        done = report['cached'] + report['translated'] + report['failed']
        fraction = done / total if total else 1
        filled = int(16 * fraction)
        bar = '#' * filled + '-' * (16 - filled)
        self.clear()
        percent = done * 100 // total if total else 100
        self.stream.write(f"[{bar}] {percent}% {done}/{total} "
                          f"新增 {report['translated']} 缓存 {report['cached']} 失败 {report['failed']}")
        self.stream.flush()
        self.visible = True

    def close(self):
        if self.visible:
            self.stream.write('\n')
            self.stream.flush()
            self.visible = False
