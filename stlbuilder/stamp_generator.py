"""Generate raised-letter leather stamp meshes and export STL."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cadquery as cq

from stlbuilder.fonts import resolve_font
from stlbuilder.geometry_utils import apply_mirror, build_base_plate


@dataclass
class StampSettings:
    text: str
    font_family: str = "Arial"
    font_path: str | None = None
    font_size: float = 12.0
    imprint_depth: float = 2.0
    base_thickness: float = 5.0
    margin: float = 4.0
    mirror_for_leather: bool = True
    # Stamp face at Z=0; letters extrude +Z; base extends -Z


class StampGenerationError(Exception):
    pass


def _validate_settings(settings: StampSettings) -> None:
    if not settings.text.strip():
        raise StampGenerationError("Enter some text for the stamp.")
    if settings.font_size <= 0:
        raise StampGenerationError("Font size must be greater than zero.")
    if settings.imprint_depth <= 0:
        raise StampGenerationError("Imprint depth must be greater than zero.")
    if settings.base_thickness <= 0:
        raise StampGenerationError("Base thickness must be greater than zero.")
    if settings.margin < 0:
        raise StampGenerationError("Margin cannot be negative.")


def build_stamp(settings: StampSettings) -> cq.Workplane:
    """Build a stamp: base plate + raised text (positive relief for leather)."""
    _validate_settings(settings)

    family, font_path = resolve_font(settings.font_family, settings.font_path)

    kwargs: dict = {
        "fontsize": settings.font_size,
        "distance": settings.imprint_depth,
        "combine": True,
        "font": family,
        "halign": "center",
        "valign": "center",
    }
    if font_path:
        kwargs["fontPath"] = font_path

    try:
        text_wp = cq.Workplane("XY").text(settings.text, **kwargs)
    except Exception as exc:
        raise StampGenerationError(
            f"Could not render text with font '{family}'. Try another font."
        ) from exc

    if settings.mirror_for_leather:
        text_wp = apply_mirror(text_wp, True)

    solid = text_wp.val()
    if solid is None:
        raise StampGenerationError("Text produced no geometry. Check font and characters.")

    bb = solid.BoundingBox()
    if bb.xlen <= 0 or bb.ylen <= 0:
        raise StampGenerationError("Text bounds are invalid.")

    margin = settings.margin
    base = build_base_plate(bb, margin, settings.base_thickness)

    # Letters sit on top of the base (base top face at Z=0).
    return base.union(text_wp)


def export_stl(model: cq.Workplane, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cq.exporters.export(model, str(out))
    return out


def model_to_trimesh(model: cq.Workplane):
    """Convert CadQuery solid to trimesh for preview (lazy import)."""
    import tempfile

    import trimesh

    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        cq.exporters.export(model, tmp_path)
        return trimesh.load(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
