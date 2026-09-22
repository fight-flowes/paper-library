"""Immutable catalog domain models."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
import re


CATALOG_COLUMNS = (
    "id",
    "year",
    "venue",
    "short_name",
    "subject",
    "title",
    "sha256",
    "parsed_at",
    "publication_date",
    "aliases",
)
PAPER_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LIBRARY_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class LibraryError(RuntimeError):
    """Raised when library configuration or its catalog is invalid."""


@dataclass(frozen=True, slots=True)
class LibraryConfig:
    id: str
    name: str
    paper_id_prefix: str


@dataclass(frozen=True, slots=True)
class LibraryDescriptor:
    root: Path
    config: LibraryConfig


@dataclass(frozen=True, slots=True)
class PaperRecord:
    id: str
    year: str
    venue: str
    short_name: str
    subject: str
    title: str
    sha256: str
    parsed_at: str
    publication_date: str
    aliases: tuple[str, ...] = ()

    @property
    def source_pdf(self) -> PurePosixPath:
        return PurePosixPath("papers", self.id, "source.pdf")

    @property
    def markdown(self) -> PurePosixPath:
        return PurePosixPath("papers", self.id, "article.md")

    @property
    def display_title(self) -> str:
        if self.short_name:
            leading = rf"^{re.escape(self.short_name)}(?=$|\W)"
            if re.search(leading, self.title, flags=re.IGNORECASE):
                return self.title
            return f"{self.short_name}: {self.title}"
        return self.title

    @property
    def list_venue(self) -> str:
        normalized = self.venue.casefold()
        if "findings of the association for computational linguistics" in normalized:
            return "Findings ACL"
        if "north american chapter" in normalized or normalized == "naacl":
            return "NAACL"
        if "association for computational linguistics" in normalized or normalized == "acl":
            return "ACL"
        return self.venue


@dataclass(frozen=True, slots=True)
class LibrarySnapshot:
    descriptor: LibraryDescriptor
    papers: tuple[PaperRecord, ...]
    warnings: tuple[str, ...] = ()

    def find(self, paper_id: str) -> PaperRecord | None:
        lookup = paper_id.casefold()
        return next(
            (
                paper
                for paper in self.papers
                if paper.id.casefold() == lookup
                or any(alias.casefold() == lookup for alias in paper.aliases)
            ),
            None,
        )

    def grouped(self) -> tuple[tuple[str, tuple[PaperRecord, ...]], ...]:
        buckets: defaultdict[str, list[PaperRecord]] = defaultdict(list)
        for paper in self.papers:
            buckets[paper.subject or "未分类"].append(paper)
        return tuple(
            (
                subject,
                tuple(
                    sorted(
                        buckets[subject],
                        key=lambda paper: (
                            _publication_date_sort_key(paper.publication_date),
                            -(int(paper.year) if paper.year.isdigit() else -1),
                            _parsed_at_sort_key(paper.parsed_at),
                            paper.display_title.casefold(),
                            paper.id.casefold(),
                        ),
                    )
                ),
            )
            for subject in sorted(buckets, key=str.casefold)
        )


def _parsed_at_sort_key(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return -parsed.timestamp()


def _publication_date_sort_key(value: str) -> float:
    try:
        return -date.fromisoformat(value).toordinal()
    except ValueError:
        return float("inf")
