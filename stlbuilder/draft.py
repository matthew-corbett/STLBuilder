"""Save and load STL Builder draft packages (.stldraft)."""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stlbuilder.stamp_generator import StampGenerationError, export_stl

DRAFT_VERSION = 1
SETTINGS_NAME = "settings.json"
STL_NAME = "stamp.stl"
IMAGE_DIR = "assets"
IMAGE_NAME = "source_image"
CUSTOM_FONT_NAME = "custom_font.ttf"


@dataclass
class DraftData:
    """Loaded draft contents ready for the UI to apply."""

    mode: str
    settings: dict[str, Any]
    stl_path: Path | None
    image_path: Path | None
    custom_font_path: Path | None
    # Keep temp dir alive while draft assets are in use.
    _workspace: tempfile.TemporaryDirectory[str] | None = None


def collect_settings_dict(
    *,
    mode: str,
    text: str,
    font_family: str,
    custom_font_path: str | None,
    font_size: float,
    imprint_depth: float,
    base_thickness: float,
    margin: float,
    mirror_for_leather: bool,
    image_path: str | None,
    width_mm: float,
    threshold: int,
    invert: bool,
    simplify: float,
    orientation: str = "horizontal",
    raised_border: bool = False,
    border_width: float = 1.5,
) -> dict[str, Any]:
    return {
        "version": DRAFT_VERSION,
        "mode": mode,
        "shared": {
            "imprint_depth": imprint_depth,
            "base_thickness": base_thickness,
            "margin": margin,
            "mirror_for_leather": mirror_for_leather,
        },
        "text": {
            "text": text,
            "font_family": font_family,
            "font_size": font_size,
            "has_custom_font": bool(custom_font_path),
            "orientation": orientation,
        },
        "image": {
            "width_mm": width_mm,
            "threshold": threshold,
            "invert": invert,
            "simplify": simplify,
            "has_image": bool(image_path),
            "original_image_name": Path(image_path).name if image_path else None,
            "raised_border": raised_border,
            "border_width": border_width,
        },
    }


def save_draft(
    path: str | Path,
    settings: dict[str, Any],
    *,
    model=None,
    image_path: str | None = None,
    custom_font_path: str | None = None,
) -> Path:
    """Write a .stldraft zip containing settings, optional STL, and assets."""
    out = Path(path)
    if out.suffix.lower() != ".stldraft":
        out = out.with_suffix(".stldraft")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / SETTINGS_NAME).write_text(
            json.dumps(settings, indent=2), encoding="utf-8"
        )

        if model is not None:
            _write_model_stl(model, root / STL_NAME)

        assets = root / IMAGE_DIR
        assets.mkdir(exist_ok=True)

        if image_path and Path(image_path).is_file():
            src = Path(image_path)
            dest = assets / f"{IMAGE_NAME}{src.suffix.lower() or '.png'}"
            shutil.copy2(src, dest)
            settings["image"]["embedded_image"] = dest.name
            (root / SETTINGS_NAME).write_text(
                json.dumps(settings, indent=2), encoding="utf-8"
            )

        if custom_font_path and Path(custom_font_path).is_file():
            dest = assets / CUSTOM_FONT_NAME
            shutil.copy2(custom_font_path, dest)
            settings["text"]["embedded_font"] = CUSTOM_FONT_NAME
            (root / SETTINGS_NAME).write_text(
                json.dumps(settings, indent=2), encoding="utf-8"
            )

        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in root.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, file_path.relative_to(root).as_posix())

    return out


def _write_model_stl(model, path: Path) -> None:
    """Export CadQuery workplane or trimesh mesh to an STL file."""
    if hasattr(model, "export") and hasattr(model, "vertices"):
        model.export(str(path))
        return
    export_stl(model, path)


def load_draft(path: str | Path) -> DraftData:
    """Extract a draft into a temp workspace and return applyable data."""
    src = Path(path)
    if not src.is_file():
        raise StampGenerationError(f"Draft not found: {path}")

    workspace = tempfile.TemporaryDirectory(prefix="stlbuilder_draft_")
    root = Path(workspace.name)

    try:
        with zipfile.ZipFile(src, "r") as zf:
            zf.extractall(root)
    except zipfile.BadZipFile as exc:
        workspace.cleanup()
        raise StampGenerationError("Draft file is not a valid .stldraft package.") from exc

    settings_path = root / SETTINGS_NAME
    if not settings_path.is_file():
        workspace.cleanup()
        raise StampGenerationError("Draft is missing settings.json.")

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        workspace.cleanup()
        raise StampGenerationError("Draft settings.json is invalid.") from exc

    if not isinstance(settings, dict):
        workspace.cleanup()
        raise StampGenerationError("Draft settings.json has an unexpected format.")

    version = settings.get("version", 1)
    if version > DRAFT_VERSION:
        workspace.cleanup()
        raise StampGenerationError(
            f"This draft (v{version}) is newer than this app supports (v{DRAFT_VERSION})."
        )

    mode = settings.get("mode", "text")
    if mode not in ("text", "image"):
        workspace.cleanup()
        raise StampGenerationError(f"Unknown draft mode: {mode}")

    stl_path = root / STL_NAME
    if not stl_path.is_file():
        stl_path = None

    image_path = None
    image_meta = settings.get("image") or {}
    embedded = image_meta.get("embedded_image")
    if embedded:
        candidate = root / IMAGE_DIR / embedded
        if candidate.is_file():
            image_path = candidate

    custom_font_path = None
    text_meta = settings.get("text") or {}
    embedded_font = text_meta.get("embedded_font")
    if embedded_font:
        candidate = root / IMAGE_DIR / embedded_font
        if candidate.is_file():
            custom_font_path = candidate

    return DraftData(
        mode=mode,
        settings=settings,
        stl_path=stl_path,
        image_path=image_path,
        custom_font_path=custom_font_path,
        _workspace=workspace,
    )
