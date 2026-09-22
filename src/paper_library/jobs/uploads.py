"""Validation and private staging for browser PDF uploads."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Protocol
from uuid import uuid4

from werkzeug.datastructures import FileStorage


class LibraryDiscovery(Protocol):
    def discover(self) -> dict[str, object]: ...


class UploadError(ValueError):
    """Raised when an upload cannot be safely staged."""


@dataclass(frozen=True, slots=True)
class UploadRequest:
    library_id: str
    subject: str
    language: str
    enrich: bool
    auto_translate: bool


@dataclass(frozen=True, slots=True)
class StagedUpload:
    upload_name: str
    original_filename: str
    size: int
    request: UploadRequest


class UploadStager:
    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int,
        libraries: LibraryDiscovery | None = None,
        reserve_bytes: int = 0,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.max_bytes = max_bytes
        self.libraries = libraries
        self.reserve_bytes = max(0, reserve_bytes)

    def stage(self, file: FileStorage, request: UploadRequest) -> StagedUpload:
        normalized = self._validate(file, request)
        self.root.mkdir(parents=True, exist_ok=True)
        upload_id = uuid4().hex
        temporary = self.root / f".{upload_id}.tmp"
        final = self.root / upload_id
        target = temporary / "source.pdf"
        size = 0
        try:
            temporary.mkdir(mode=0o700)
            with target.open("xb") as handle:
                while True:
                    chunk = file.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise UploadError("PDF 文件过大")
                    if self.reserve_bytes and shutil.disk_usage(self.root).free < self.reserve_bytes + len(chunk):
                        raise UploadError("磁盘可用空间不足，请清理空间后重试")
                    handle.write(chunk)
            with target.open("rb") as handle:
                signature = handle.read(5)
            if size < 5 or signature != b"%PDF-":
                raise UploadError("文件不是有效的 PDF")
            temporary.rename(final)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            shutil.rmtree(final, ignore_errors=True)
            raise
        return StagedUpload(
            upload_name=f"{upload_id}/source.pdf",
            original_filename=self._safe_filename(file.filename or "paper.pdf"),
            size=size,
            request=normalized,
        )

    def remove(self, upload_name: str) -> None:
        relative = PurePosixPath(upload_name)
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[1] != "source.pdf"
            or not re.fullmatch(r"[0-9a-f]{32}", relative.parts[0])
        ):
            raise UploadError("上传文件路径无效")
        directory = (self.root / relative.parts[0]).resolve()
        if directory.parent != self.root:
            raise UploadError("上传文件路径无效")
        shutil.rmtree(directory, ignore_errors=True)

    def resolve(self, upload_name: str) -> Path:
        relative = PurePosixPath(upload_name)
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[1] != "source.pdf"
            or not re.fullmatch(r"[0-9a-f]{32}", relative.parts[0])
        ):
            raise UploadError("上传文件路径无效")
        result = (self.root / relative.parts[0] / relative.parts[1]).resolve()
        if result.parent.parent != self.root:
            raise UploadError("上传文件路径无效")
        return result

    def _validate(self, file: FileStorage, request: UploadRequest) -> UploadRequest:
        filename = self._safe_filename(file.filename or "")
        if not filename.casefold().endswith(".pdf"):
            raise UploadError("请选择 PDF 文件")
        subject = re.sub(r"[\t\r\n]+", " ", request.subject)
        subject = re.sub(r" +", " ", subject).strip()
        if not subject or len(subject) > 120:
            raise UploadError("主题必须为 1 到 120 个字符")
        if request.language not in {"en", "ch"}:
            raise UploadError("不支持的文档语言")
        if self.libraries is not None and request.library_id not in self.libraries.discover():
            raise UploadError("目标论文库不存在")
        return replace(request, subject=subject)

    @staticmethod
    def _safe_filename(filename: str) -> str:
        return filename.replace("\\", "/").rsplit("/", 1)[-1]
