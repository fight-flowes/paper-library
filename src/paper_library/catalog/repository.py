"""Discover and load normalized paper libraries."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import tomllib
from typing import Any

from .models import (
    CATALOG_COLUMNS,
    LIBRARY_ID_PATTERN,
    PAPER_KEY_PATTERN,
    LibraryConfig,
    LibraryDescriptor,
    LibraryError,
    LibrarySnapshot,
    PaperRecord,
)
from .paths import PaperPaths


class LibraryRepository:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def discover(self) -> dict[str, LibraryDescriptor]:
        try:
            children = sorted(self.root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise LibraryError(f"无法读取论文库目录：{self.root}") from exc
        result: dict[str, LibraryDescriptor] = {}
        for child in children:
            if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
                continue
            config_path = child / "library.toml"
            catalog_path = child / "catalog.tsv"
            if not config_path.is_file() and not catalog_path.is_file():
                continue
            if config_path.is_symlink() or catalog_path.is_symlink():
                continue
            config = _read_config(config_path)
            if child.name != config.id:
                raise LibraryError(f"论文库目录名必须与 id 一致：{child.name} != {config.id}")
            if config.id in result:
                raise LibraryError(f"论文库 ID 重复：{config.id}")
            result[config.id] = LibraryDescriptor(child.resolve(), config)
        return result

    def load(self, library_id: str) -> LibrarySnapshot:
        descriptor = self.discover().get(library_id)
        if descriptor is None:
            raise LibraryError(f"未找到论文库：{library_id}")
        return load_library(descriptor.root, descriptor=descriptor)

    def find_paper(self, library_id: str, paper_id: str) -> PaperRecord | None:
        return self.load(library_id).find(paper_id)

    def validate_all(self) -> tuple[str, ...]:
        warnings: list[str] = []
        keys: dict[str, str] = {}
        for library_id in self.discover():
            try:
                snapshot = self.load(library_id)
            except LibraryError as exc:
                warnings.append(f"{library_id}: {exc}")
                continue
            warnings.extend(f"{library_id}: {warning}" for warning in snapshot.warnings)
            for paper in snapshot.papers:
                folded = paper.id.casefold()
                if folded in keys:
                    warnings.append(f"跨论文库重复论文 ID：{paper.id}（{keys[folded]} / {library_id}）")
                else:
                    keys[folded] = library_id
                paths = PaperPaths.for_id(snapshot.descriptor.root, paper.id)
                for label, path in (("PDF", paths.pdf), ("Markdown", paths.markdown), ("metadata", paths.metadata)):
                    if not path.is_file() or path.is_symlink():
                        warnings.append(f"{library_id}/{paper.id}: {label} 文件缺失")
                if not paths.images.is_dir() or paths.images.is_symlink():
                    warnings.append(f"{library_id}/{paper.id}: images 目录缺失")
        return tuple(warnings)


def load_library(root: Path, *, descriptor: LibraryDescriptor | None = None) -> LibrarySnapshot:
    root = root.expanduser().resolve()
    descriptor = descriptor or LibraryDescriptor(root, _read_config(root / "library.toml"))
    try:
        handle = (root / "catalog.tsv").open("r", encoding="utf-8", newline="")
    except (OSError, UnicodeError) as exc:
        raise LibraryError("无法读取 catalog.tsv") from exc
    papers: list[PaperRecord] = []
    warnings: list[str] = []
    seen: set[str] = set()
    with handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if tuple(reader.fieldnames or ()) != CATALOG_COLUMNS:
            raise LibraryError("catalog.tsv 表头不符合新版格式")
        for line_number, row in enumerate(reader, start=2):
            try:
                paper = _parse_row(row)
                for key in (paper.id, *paper.aliases):
                    if key.casefold() in seen:
                        raise ValueError(f"论文 ID 或别名重复：{key}")
                seen.update(key.casefold() for key in (paper.id, *paper.aliases))
                papers.append(paper)
            except (TypeError, ValueError) as exc:
                warnings.append(f"目录第 {line_number} 行已跳过：{exc}")
    return LibrarySnapshot(descriptor, tuple(papers), tuple(warnings))


def read_metadata(library_root: Path, paper: PaperRecord) -> tuple[dict[str, Any], tuple[str, ...]]:
    path = PaperPaths.for_id(library_root, paper.id).metadata
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, ("metadata.json 缺失或无法读取，已使用目录信息。",)
    if not isinstance(value, dict):
        return {}, ("metadata.json 不是 JSON 对象，已使用目录信息。",)
    return value, ()


def _read_config(path: Path) -> LibraryConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise LibraryError(f"无法读取 library.toml：{path}") from exc
    if raw.get("schema_version") != 1:
        raise LibraryError("library.toml schema_version 必须为 1")
    library_id = raw.get("id")
    name = raw.get("name")
    prefix = raw.get("paper_id_prefix")
    if not isinstance(library_id, str) or not LIBRARY_ID_PATTERN.fullmatch(library_id):
        raise LibraryError("library.toml id 格式无效")
    if not isinstance(name, str) or not name.strip():
        raise LibraryError("library.toml name 不能为空")
    if not isinstance(prefix, str) or not re_full_prefix(prefix):
        raise LibraryError("library.toml paper_id_prefix 格式无效")
    return LibraryConfig(library_id, name.strip(), prefix)


def re_full_prefix(value: str) -> bool:
    return bool(value) and value.isascii() and value.isalnum() and value == value.casefold()


def _parse_row(row: dict[str | None, str | None]) -> PaperRecord:
    if None in row:
        raise ValueError("列数与表头不一致")
    values = {column: (row.get(column) or "").strip() for column in CATALOG_COLUMNS}
    for required in ("id", "title"):
        if not values[required]:
            raise ValueError(f"缺少必填字段：{required}")
    aliases = tuple(value.strip() for value in values["aliases"].split("|") if value.strip())
    keys = (values["id"], *aliases)
    if any(not PAPER_KEY_PATTERN.fullmatch(key) for key in keys):
        raise ValueError("论文 ID 或别名格式无效")
    if len({key.casefold() for key in keys}) != len(keys):
        raise ValueError("论文 ID 与别名不能重复")
    return PaperRecord(
        id=values["id"], year=values["year"], venue=values["venue"],
        short_name=values["short_name"], subject=values["subject"], title=values["title"],
        sha256=values["sha256"], parsed_at=values["parsed_at"],
        publication_date=values["publication_date"], aliases=aliases,
    )
