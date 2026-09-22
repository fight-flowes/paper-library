"""Filesystem boundaries for normalized paper packages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .models import PaperRecord


IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


@dataclass(frozen=True, slots=True)
class PaperPaths:
    root: Path
    pdf: Path
    markdown: Path
    metadata: Path
    images: Path

    @classmethod
    def for_id(cls, library_root: Path, paper_id: str) -> "PaperPaths":
        root = _contained(library_root, PurePosixPath("papers", paper_id))
        return cls(root, root / "source.pdf", root / "article.md", root / "metadata.json", root / "images")


def resolve_library_path(root: Path, relative: PurePosixPath, suffix: str | None = None) -> Path:
    raw = str(relative)
    if not raw or "\x00" in raw or "\\" in raw or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("路径必须位于论文库内")
    if suffix and relative.suffix.casefold() != suffix.casefold():
        raise ValueError(f"路径必须使用 {suffix} 后缀")
    return _contained(root, relative)


def resolve_paper_asset(library_root: Path, paper: PaperRecord, asset_path: str) -> Path:
    relative = PurePosixPath(asset_path)
    if (
        not asset_path
        or "\x00" in asset_path
        or "\\" in asset_path
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix.casefold() not in IMAGE_SUFFIXES
    ):
        raise ValueError("资源路径格式无效")
    package = PaperPaths.for_id(library_root, paper.id).root
    candidate = _contained(package, relative)
    if not candidate.is_file():
        raise ValueError("论文资源不存在")
    return candidate


def _contained(root: Path, relative: PurePosixPath) -> Path:
    base = root.resolve()
    candidate = base.joinpath(*relative.parts).resolve(strict=False)
    if not candidate.is_relative_to(base):
        raise ValueError("路径解析后位于论文库之外")
    return candidate
