"""Source-based dashboard categories and archive/group summaries."""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable, Literal, Protocol

ArchiveSourceName = Literal["downloads", "vortex"]


class ArchiveSource(Protocol):
    source: ArchiveSourceName


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
    source: ArchiveSourceName
    folder: Path
    exclude: tuple[Path, ...] = ()
    warning: str = ""


def build_scan_plan(
    downloads: Path | None,
    vortex: Path | None,
) -> list[ScanRoot]:
    """Avoid scanning the same physical files under both source labels."""
    downloads_root = _resolve_root(downloads)
    vortex_root = _resolve_root(vortex)

    if downloads_root is None:
        return [ScanRoot("vortex", vortex_root)] if vortex_root is not None else []
    if vortex_root is None:
        return [ScanRoot("downloads", downloads_root)]

    downloads_key = _path_key(downloads_root)
    vortex_key = _path_key(vortex_root)
    if downloads_key == vortex_key:
        return [ScanRoot(
            "downloads",
            downloads_root,
            warning="downloads и vortex указывают на одну папку; сканирую её один раз как Downloads",
        )]

    vortex_inside_downloads = _is_inside(vortex_key, downloads_key)
    downloads_inside_vortex = _is_inside(downloads_key, vortex_key)

    downloads_exclude = (vortex_root,) if vortex_inside_downloads else ()
    vortex_exclude = (downloads_root,) if downloads_inside_vortex else ()
    return [
        ScanRoot("downloads", downloads_root, downloads_exclude),
        ScanRoot("vortex", vortex_root, vortex_exclude),
    ]


def _resolve_root(folder: Path | None) -> Path | None:
    return Path(os.path.realpath(folder)) if folder is not None else None


def _path_key(folder: Path) -> str:
    return os.path.normcase(str(folder))


def _is_inside(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath([path, parent]) == parent
    except ValueError:
        return False


def source_categories(sources: Iterable[str]) -> set[str]:
    """Return the Vortex-only filter for archive groups."""
    found = set(sources)
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
        sources = _group_sources(group)
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


def _group_sources(group: GroupFiles) -> set[ArchiveSourceName]:
    return {archive.source for archive in group.files}
