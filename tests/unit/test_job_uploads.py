from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from werkzeug.datastructures import FileStorage

from paper_library.jobs.uploads import UploadError, UploadRequest, UploadStager


class Libraries:
    def discover(self) -> dict[str, object]:
        return {"whitespace": object(), "mechanism-interpretability": object()}


def valid_request(**changes: object) -> UploadRequest:
    values = {
        "library_id": "whitespace",
        "subject": "related-work",
        "language": "en",
        "enrich": True,
        "auto_translate": True,
    }
    values.update(changes)
    return UploadRequest(**values)


def make_stager(tmp_path: Path, *, max_bytes: int = 1024) -> UploadStager:
    return UploadStager(tmp_path / "uploads", max_bytes=max_bytes, libraries=Libraries())


def test_stage_rejects_stream_larger_than_limit_and_removes_partial(tmp_path: Path) -> None:
    stager = make_stager(tmp_path, max_bytes=8)
    file = FileStorage(stream=BytesIO(b"%PDF-123456789"), filename="paper.pdf")

    with pytest.raises(UploadError, match="过大"):
        stager.stage(file, valid_request())

    assert list((tmp_path / "uploads").glob("**/*")) == []


@pytest.mark.parametrize(
    ("filename", "payload"),
    [("paper.txt", b"%PDF-content"), ("paper.pdf", b"not-a-pdf")],
)
def test_stage_requires_suffix_and_signature(
    tmp_path: Path, filename: str, payload: bytes
) -> None:
    with pytest.raises(UploadError):
        make_stager(tmp_path).stage(
            FileStorage(BytesIO(payload), filename=filename), valid_request()
        )


def test_stage_normalizes_subject_and_ignores_hostile_filename(tmp_path: Path) -> None:
    staged = make_stager(tmp_path).stage(
        FileStorage(BytesIO(b"%PDF-content"), filename="../../secret.PDF"),
        valid_request(subject="  Model\n  behavior\tanalysis "),
    )

    assert staged.request.subject == "Model behavior analysis"
    assert staged.original_filename == "secret.PDF"
    assert staged.upload_name.endswith("/source.pdf")
    assert "secret" not in staged.upload_name
    resolved = (tmp_path / "uploads" / staged.upload_name).resolve()
    assert resolved.is_relative_to((tmp_path / "uploads").resolve())
    assert resolved.read_bytes() == b"%PDF-content"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"library_id": "missing"}, "论文库"),
        ({"subject": "\n\t"}, "主题"),
        ({"subject": "x" * 121}, "主题"),
        ({"language": "fr"}, "语言"),
    ],
)
def test_stage_rejects_invalid_request(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    with pytest.raises(UploadError, match=message):
        make_stager(tmp_path).stage(
            FileStorage(BytesIO(b"%PDF-content"), filename="paper.pdf"),
            valid_request(**changes),
        )


def test_remove_deletes_only_a_staged_upload(tmp_path: Path) -> None:
    stager = make_stager(tmp_path)
    staged = stager.stage(
        FileStorage(BytesIO(b"%PDF-content"), filename="paper.pdf"), valid_request()
    )

    stager.remove(staged.upload_name)

    assert list((tmp_path / "uploads").glob("**/*")) == []
    with pytest.raises(UploadError):
        stager.remove("../../outside.pdf")


def test_low_disk_upload_is_rejected_without_partial_file(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from io import BytesIO
    from werkzeug.datastructures import FileStorage
    from paper_library.jobs.uploads import UploadStager,UploadRequest,UploadError
    import pytest
    monkeypatch.setattr('paper_library.jobs.uploads.shutil.disk_usage',lambda path:SimpleNamespace(free=10))
    stager=UploadStager(tmp_path/'uploads',max_bytes=1000,reserve_bytes=100)
    with pytest.raises(UploadError,match='磁盘'):
        stager.stage(FileStorage(stream=BytesIO(b'%PDF-example'),filename='paper.pdf'),UploadRequest('lib','topic','en',True,True))
    assert not list((tmp_path/'uploads').iterdir())
