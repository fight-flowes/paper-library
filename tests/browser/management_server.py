from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import Lock, Thread
import time

from paper_library.catalog import CATALOG_COLUMNS, PaperPaths
from paper_library.config import ProjectPaths, Settings
from paper_library.jobs import JobProgress, JobStage
from paper_library.web.app import create_app


class BrowserRunner:
    def __init__(self) -> None:
        self.jobs = None
        self.settings = None
        self.lock = Lock()

    def start(self) -> None:
        return None

    def stop(self, timeout=None) -> None:
        return None

    def wake(self) -> None:
        Thread(target=self._work, daemon=True).start()

    def cancel(self, job_id):
        return self.jobs.request_cancel(job_id)

    def _work(self) -> None:
        with self.lock:
            job = self.jobs.claim_next("browser-runner", lease_seconds=30)
            if job is None:
                return
            self.jobs.update(job.id, JobStage.PARSING, activity={"message": "正在识别版面"})
            time.sleep(1.2)
            self.jobs.update(job.id, JobStage.REPAIRING, activity={"done": 1, "total": 3, "unit": "处"})
            time.sleep(1.2)
            self.jobs.update(job.id, JobStage.ENRICHING, activity={"message": "正在核对论文信息"})
            time.sleep(1.2)
            library = self.settings.paths.libraries / job.library_id
            paper_id = "ws-p0001"
            paths = PaperPaths.for_id(library, paper_id)
            paths.root.mkdir(parents=True, exist_ok=True)
            paths.pdf.write_bytes(b"%PDF-browser")
            paths.markdown.write_text("# Browser paper\n\nEnglish paragraph.\n\n" + "\n\n".join(f"Reading paragraph {i}. " + "Longer body text for stable reading position. " * 12 for i in range(25)), encoding="utf-8")
            paths.metadata.write_text("{}\n", encoding="utf-8")
            paths.images.mkdir(exist_ok=True)
            digest = hashlib.sha256(paths.pdf.read_bytes()).hexdigest()
            with (library / "catalog.tsv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
                writer.writerow(CATALOG_COLUMNS)
                writer.writerow([paper_id, "2026", "", "", job.subject, "Browser paper",
                                 digest, "2026-09-20T00:00:00+00:00", "", ""])
            self.jobs.update(job.id, JobStage.PUBLISHED, paper_id=paper_id)
            from paper_library.structure.document import build_blocks, source_hash, VERSION
            source = paths.markdown.read_text()
            (paths.root / "document-structure.json").write_text(json.dumps({
                "version": VERSION, "source_sha256": source_hash(source),
                "annotations": [{"id": b["id"], "role": "abstract" if "English paragraph" in b["text"] else "title"}
                                for b in build_blocks(source)]}))
            self.jobs.update(job.id, JobStage.STRUCTURING, activity={"done": 1, "total": 2, "unit": "块"})
            time.sleep(1.2)
            if job.auto_translate:
                self.jobs.update(job.id, JobStage.TRANSLATING, JobProgress(total=2, pending=2))
                time.sleep(1.2)
                self.jobs.update(
                    job.id, JobStage.TRANSLATING,
                    JobProgress(total=2, cached=0, translated=2, pending=0),
                )
            self.jobs.complete(job.id)


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="paper-management-browser-"))
    paths = ProjectPaths.from_root(root)
    library = paths.libraries / "whitespace"
    library.mkdir(parents=True)
    (library / "library.toml").write_text(
        'schema_version = 1\nid = "whitespace"\nname = "Whitespace"\n'
        'paper_id_prefix = "ws"\n', encoding="utf-8"
    )
    (library / "catalog.tsv").write_text("\t".join(CATALOG_COLUMNS) + "\n", encoding="utf-8")
    settings = Settings.load(paths, {"PAPER_LIBRARY_PORT": os.getenv("PAPER_MANAGEMENT_TEST_PORT", "18021")})
    runner = BrowserRunner()
    app = create_app(settings, job_runner=runner, start_jobs=False)
    runner.jobs = app.extensions["job_repository"]
    runner.settings = settings
    app.run(host="127.0.0.1", port=settings.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
