"""Source-based dashboard categories and archive/group summaries."""

from dataclasses import dataclass
import os
from pathlib import Path
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


@dataclass(frozen=True)
class ScanRoot:
    source: str
    folder: Path
    exclude: tuple[Path, ...] = ()
    warning: str = ""


def build_scan_plan(
    downloads: Path | None,
    vortex: Path | None,
) -> list[ScanRoot]:
    """Avoid scanning the same physical files under both source labels."""
    downloads_root = Path(os.path.realpath(downloads)) if downloads else None
    vortex_root = Path(os.path.realpath(vortex)) if vortex else None

    if downloads_root is None:
        return [ScanRoot("vortex", vortex_root)] if vortex_root else []
    if vortex_root is None:
        return [ScanRoot("downloads", downloads_root)]

    downloads_key = os.path.normcase(str(downloads_root))
    vortex_key = os.path.normcase(str(vortex_root))
    if downloads_key == vortex_key:
        return [ScanRoot(
            "downloads",
            downloads_root,
            warning="downloads и vortex указывают на одну папку; сканирую её один раз как Downloads",
        )]

    try:
        vortex_inside_downloads = os.path.commonpath([downloads_key, vortex_key]) == downloads_key
        downloads_inside_vortex = os.path.commonpath([downloads_key, vortex_key]) == vortex_key
    except ValueError:
        vortex_inside_downloads = False
        downloads_inside_vortex = False

    downloads_exclude = (vortex_root,) if vortex_inside_downloads else ()
    vortex_exclude = (downloads_root,) if downloads_inside_vortex else ()
    return [
        ScanRoot("downloads", downloads_root, downloads_exclude),
        ScanRoot("vortex", vortex_root, vortex_exclude),
    ]


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
