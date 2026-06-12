"""System font discovery for the stamp designer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from matplotlib import font_manager


@dataclass(frozen=True)
class FontOption:
    display_name: str
    family: str
    path: str | None


def list_system_fonts() -> list[FontOption]:
    """Return sorted unique fonts (prefer files with paths for CadQuery)."""
    by_name: dict[str, FontOption] = {}
    for entry in font_manager.fontManager.ttflist:
        name = entry.name.strip()
        if not name:
            continue
        path = entry.fname
        existing = by_name.get(name)
        if existing is None or (existing.path is None and path):
            by_name[name] = FontOption(
                display_name=name,
                family=name,
                path=path if path and Path(path).is_file() else None,
            )
    return sorted(by_name.values(), key=lambda f: f.display_name.lower())


def resolve_font(family: str, custom_path: str | None = None) -> tuple[str, str | None]:
    """Map UI selection to CadQuery font + optional fontPath."""
    if custom_path and Path(custom_path).is_file():
        return Path(custom_path).stem, custom_path

    for opt in list_system_fonts():
        if opt.family == family:
            return opt.family, opt.path

    return family, None
