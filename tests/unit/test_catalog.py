import csv
from pathlib import Path

import pytest

from paper_library.catalog import (
    CATALOG_COLUMNS,
    LibraryError,
    LibraryRepository,
    PaperPaths,
)


def write_library(root: Path, library_id: str = "alpha", prefix: str = "a") -> Path:
    library = root / library_id
    library.mkdir(parents=True)
    (library / "library.toml").write_text(
        f'schema_version = 1\nid = "{library_id}"\nname = "Alpha"\n'
        f'paper_id_prefix = "{prefix}"\n',
        encoding="utf-8",
    )
    with (library / "catalog.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(CATALOG_COLUMNS)
        writer.writerow(["a-p0001", "2026", "ACL", "Test", "topic", "Title", "abc", "now", "2026-01-01", "legacy"])
    paths = PaperPaths.for_id(library, "a-p0001")
    paths.root.mkdir(parents=True)
    paths.pdf.write_bytes(b"pdf")
    paths.markdown.write_text("# Title", encoding="utf-8")
    paths.metadata.write_text("{}", encoding="utf-8")
    paths.images.mkdir()
    return library


def test_repository_discovers_configured_libraries_and_fixed_paths(tmp_path: Path) -> None:
    library = write_library(tmp_path)
    repository = LibraryRepository(tmp_path)

    discovered = repository.discover()
    snapshot = repository.load("alpha")

    assert discovered["alpha"].root == library
    assert discovered["alpha"].config.name == "Alpha"
    assert snapshot.find("legacy").id == "a-p0001"
    assert snapshot.papers[0].source_pdf.as_posix() == "papers/a-p0001/source.pdf"
    assert snapshot.papers[0].markdown.as_posix() == "papers/a-p0001/article.md"
    assert repository.validate_all() == ()


def test_directory_must_match_stable_library_id(tmp_path: Path) -> None:
    library = write_library(tmp_path, "wrong-name")
    (library / "library.toml").write_text(
        'schema_version = 1\nid = "other"\nname = "Other"\npaper_id_prefix = "o"\n',
        encoding="utf-8",
    )

    with pytest.raises(LibraryError, match="目录名"):
        LibraryRepository(tmp_path).discover()


def test_cross_library_canonical_ids_are_rejected(tmp_path: Path) -> None:
    write_library(tmp_path, "alpha")
    second = write_library(tmp_path, "beta", "b")
    catalog = (second / "catalog.tsv").read_text(encoding="utf-8")
    (second / "catalog.tsv").write_text(catalog.replace("a-p0001", "A-P0001"), encoding="utf-8")

    warnings = LibraryRepository(tmp_path).validate_all()

    assert any("跨论文库重复" in warning for warning in warnings)


def test_symlink_library_is_not_discovered(tmp_path: Path) -> None:
    library = write_library(tmp_path, "alpha")
    (tmp_path / "linked").symlink_to(library, target_is_directory=True)

    assert tuple(LibraryRepository(tmp_path).discover()) == ("alpha",)
