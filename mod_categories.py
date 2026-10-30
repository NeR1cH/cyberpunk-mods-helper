"""Source-based dashboard categories and archive/group summaries."""

from dataclasses import dataclass
from typing import Iterable, Protocol


class ArchiveSource(Protocol):
    source: str


class GroupFiles(Protocol):
    files: list[ArchiveSource]


@dataclass(frozen=True)
class SourceSummary:
    downloads_archives: int
    vortex_archives: int
    downloads_only_groups: int
    vortex_only_groups: int
    both_source_groups: int


def source_categories(sources: Iterable[str]) -> set[str]:
    """Return mutually exclusive source filters for a group's archive locations."""
    found = set(sources)
    if found == {"downloads"}:
        return {"local"}
    if found == {"vortex"}:
        return {"vortex"}
    return set()


def summarize_sources(
    archives: Iterable[ArchiveSource],
    groups: Iterable[GroupFiles],
) -> SourceSummary:
    """Count archives per folder and groups by the folders that contain their files."""
    downloads_archives = 0
    vortex_archives = 0
    for archive in archives:
        if archive.source == "downloads":
            downloads_archives += 1
        elif archive.source == "vortex":
            vortex_archives += 1

    downloads_only_groups = 0
    vortex_only_groups = 0
    both_source_groups = 0
    for group in groups:
        sources = {archive.source for archive in group.files}
        if sources == {"downloads"}:
            downloads_only_groups += 1
        elif sources == {"vortex"}:
            vortex_only_groups += 1
        elif sources == {"downloads", "vortex"}:
            both_source_groups += 1

    return SourceSummary(
        downloads_archives=downloads_archives,
        vortex_archives=vortex_archives,
        downloads_only_groups=downloads_only_groups,
        vortex_only_groups=vortex_only_groups,
        both_source_groups=both_source_groups,
    )
