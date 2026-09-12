"""Parametric and silhouette valet-tray wet-form molds (male plug + optional female)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import cadquery as cq
from shapely.geometry import Polygon
from shapely.ops import unary_union

from stlbuilder.image_stamp import (
    _binary_mask,
    _clean_ring,
    _contours_to_polygons,
    _load_grayscale_array,
    _merge_polygons,
    _scale_polygons,
    _smooth_mask_edges,
    _smooth_polygons,
)
from stlbuilder.stamp_generator import StampGenerationError
from stlbuilder.svg_stamp import (
    flip_y_polygons,
    invert_polygons,
    is_svg_path,
    scale_polygons_to_width,
    svg_to_polygons,
)

SHAPE_RECT = "rect"
SHAPE_ROUNDED_RECT = "rounded_rect"
SHAPE_OVAL = "oval"
SHAPE_SILHOUETTE = "silhouette"
VALID_SHAPES = (SHAPE_RECT, SHAPE_ROUNDED_RECT, SHAPE_OVAL, SHAPE_SILHOUETTE)

# Female collar plate thickness (mm) — stiff enough for clamps.
_FEMALE_THICKNESS_MM = 10.0
# Default flange plate thickness when flange_mm > 0.
_FLANGE_THICKNESS_MM = 3.0
# Minimum clamp margin around female opening.
_FEMALE_WALL_MM = 18.0


@dataclass
class ValetMoldSettings:
    shape: str = SHAPE_ROUNDED_RECT
    length_mm: float = 180.0
    width_mm: float = 130.0
    height_mm: float = 18.0
    corner_radius_mm: float = 12.0
    draft_deg: float = 2.0
    top_fillet_mm: float = 2.0
    flange_mm: float = 8.0
    leather_thickness_mm: float = 3.2  # ~8oz
    include_female: bool = False
    # Silhouette import
    image_path: str | None = None
    target_width_mm: float = 180.0
    threshold: int = 128
    invert: bool = False
    simplify: float = 0.0
    max_pixels: int = 3200
    edge_smooth_px: int = 3


@dataclass
class ValetMoldResult:
    male: cq.Workplane
    female: cq.Workplane | None = None


def _validate_settings(settings: ValetMoldSettings) -> None:
    if settings.shape not in VALID_SHAPES:
        raise StampGenerationError(
            f"Unknown mold shape '{settings.shape}'. "
            f"Use one of: {', '.join(VALID_SHAPES)}."
        )
    if settings.height_mm <= 0:
        raise StampGenerationError("Mold height must be greater than zero.")
    if settings.draft_deg < 0 or settings.draft_deg > 15:
        raise StampGenerationError("Draft angle must be between 0 and 15 degrees.")
    if settings.top_fillet_mm < 0:
        raise StampGenerationError("Top fillet cannot be negative.")
    if settings.flange_mm < 0:
        raise StampGenerationError("Flange width cannot be negative.")
    if settings.leather_thickness_mm <= 0:
        raise StampGenerationError("Leather thickness must be greater than zero.")
    if settings.shape == SHAPE_SILHOUETTE:
        if not settings.image_path:
            raise StampGenerationError("Import a silhouette image or SVG for mold shape.")
        if not Path(settings.image_path).is_file():
            raise StampGenerationError(f"Silhouette not found: {settings.image_path}")
        if settings.target_width_mm <= 0:
            raise StampGenerationError("Silhouette width must be greater than zero.")
        if not 0 <= settings.threshold <= 255:
            raise StampGenerationError("Threshold must be between 0 and 255.")
    else:
        if settings.length_mm <= 0 or settings.width_mm <= 0:
            raise StampGenerationError("Length and width must be greater than zero.")
        if settings.shape == SHAPE_ROUNDED_RECT:
            max_r = min(settings.length_mm, settings.width_mm) / 2.0 - 0.1
            if settings.corner_radius_mm < 0:
                raise StampGenerationError("Corner radius cannot be negative.")
            if settings.corner_radius_mm > max_r:
                raise StampGenerationError(
                    f"Corner radius must be ≤ {max_r:.1f} mm for this size."
                )


def _solid_exterior(poly: Polygon) -> Polygon:
    """Drop holes so the mold is a solid plug."""
    if poly.is_empty:
        return poly
    geom = Polygon(poly.exterior)
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    return geom


def _largest_polygon(polygons: list[Polygon]) -> Polygon:
    solids = [_solid_exterior(p) for p in polygons if p is not None and not p.is_empty]
    solids = [p for p in solids if p.geom_type == "Polygon" and p.area > 1e-4]
    if not solids:
        raise StampGenerationError(
            "No mold outline found. Adjust threshold/invert or use a clearer silhouette."
        )
    merged = unary_union(solids)
    if merged.geom_type == "Polygon":
        return _solid_exterior(merged)
    if merged.geom_type == "MultiPolygon":
        return _solid_exterior(max(merged.geoms, key=lambda g: g.area))
    raise StampGenerationError("Silhouette did not produce a usable outline.")


def _silhouette_polygon(settings: ValetMoldSettings) -> Polygon:
    path = settings.image_path
    assert path is not None

    if is_svg_path(path):
        polygons, src_w, src_h = svg_to_polygons(path)
        if settings.invert:
            polygons = invert_polygons(polygons, src_w, src_h)
            if not polygons:
                raise StampGenerationError("Invert left no silhouette shapes.")
        polygons = flip_y_polygons(polygons, src_h)
        polygons, _height_mm = scale_polygons_to_width(
            polygons, src_w, src_h, settings.target_width_mm
        )
        polygons = _smooth_polygons(
            polygons,
            0.25,
            0,
            max_ring_points=360,
            letter_smooth_passes=2,
            letter_max_edge_mm=0.15,
            letter_max_points=400,
        )
        polygons = _merge_polygons(polygons)
        return _largest_polygon(polygons)

    gray = _load_grayscale_array(path, settings.max_pixels)
    mask = _binary_mask(gray, settings.threshold, settings.invert)
    mask = _smooth_mask_edges(mask, settings.edge_smooth_px)
    polygons = _contours_to_polygons(mask, settings.simplify)
    if not polygons:
        raise StampGenerationError(
            "No mold outline found. Adjust threshold/invert or use a higher-contrast image."
        )
    polygons = _scale_polygons(polygons, settings.target_width_mm, mask.shape)
    polygons = _smooth_polygons(
        polygons,
        0.25,
        0,
        max_ring_points=360,
        letter_smooth_passes=2,
        letter_max_edge_mm=0.15,
        letter_max_points=400,
    )
    polygons = _merge_polygons(polygons)
    return _largest_polygon(polygons)


def _parametric_bottom_polygon(settings: ValetMoldSettings) -> Polygon:
    l = settings.length_mm
    w = settings.width_mm
    if settings.shape == SHAPE_OVAL:
        # Approximate ellipse with a dense polygon for flange / female ops.
        n = 96
        pts = [
            (
                (l / 2.0) * math.cos(2 * math.pi * i / n),
                (w / 2.0) * math.sin(2 * math.pi * i / n),
            )
            for i in range(n)
        ]
        return Polygon(pts)

    if settings.shape == SHAPE_RECT or settings.corner_radius_mm <= 0:
        return Polygon(
            [
                (-l / 2, -w / 2),
                (l / 2, -w / 2),
                (l / 2, w / 2),
                (-l / 2, w / 2),
            ]
        )

    r = min(settings.corner_radius_mm, min(l, w) / 2.0 - 0.05)
    # Build rounded rect via shapely buffer of inset rectangle.
    inset = Polygon(
        [
            (-l / 2 + r, -w / 2 + r),
            (l / 2 - r, -w / 2 + r),
            (l / 2 - r, w / 2 - r),
            (-l / 2 + r, w / 2 - r),
        ]
    )
    return inset.buffer(r, resolution=16)


def _bottom_outline(settings: ValetMoldSettings) -> Polygon:
    if settings.shape == SHAPE_SILHOUETTE:
        return _silhouette_polygon(settings)
    return _parametric_bottom_polygon(settings)


def _extrude_tapered_wire(
    outer: list[tuple[float, float]],
    height: float,
    draft_deg: float,
) -> cq.Workplane:
    if len(outer) < 3:
        raise StampGenerationError("Mold outline needs at least 3 points.")
    try:
        wp = cq.Workplane("XY").polyline(outer).close()
        if draft_deg > 1e-6:
            return wp.extrude(height, taper=draft_deg)
        return wp.extrude(height)
    except Exception as exc:
        raise StampGenerationError(
            "Could not extrude mold outline. Simplify the silhouette or reduce draft."
        ) from exc


def _extrude_parametric_male(settings: ValetMoldSettings) -> cq.Workplane:
    l = settings.length_mm
    w = settings.width_mm
    h = settings.height_mm
    taper = settings.draft_deg if settings.draft_deg > 1e-6 else None

    try:
        if settings.shape == SHAPE_OVAL:
            wp = cq.Workplane("XY").ellipse(l / 2.0, w / 2.0)
            return wp.extrude(h, taper=taper) if taper else wp.extrude(h)

        if settings.shape == SHAPE_ROUNDED_RECT and settings.corner_radius_mm > 0:
            r = min(settings.corner_radius_mm, min(l, w) / 2.0 - 0.05)
            sketch = cq.Sketch().rect(l, w).vertices().fillet(r)
            wp = cq.Workplane("XY").placeSketch(sketch)
            return wp.extrude(h, taper=taper) if taper else wp.extrude(h)

        wp = cq.Workplane("XY").rect(l, w)
        return wp.extrude(h, taper=taper) if taper else wp.extrude(h)
    except Exception as exc:
        # Fallback: polygon extrusion (handles odd CadQuery sketch failures).
        poly = _parametric_bottom_polygon(settings)
        outer = _clean_ring(poly.exterior.coords)
        return _extrude_tapered_wire(outer, h, settings.draft_deg)


def _apply_top_fillet(model: cq.Workplane, fillet_mm: float) -> cq.Workplane:
    if fillet_mm <= 0.05:
        return model
    try:
        return model.edges(">Z").fillet(fillet_mm)
    except Exception:
        try:
            # Slightly smaller fillet if the requested size fails on sharp corners.
            return model.edges(">Z").fillet(max(0.5, fillet_mm * 0.5))
        except Exception:
            return model


def _add_flange(
    male: cq.Workplane,
    bottom: Polygon,
    flange_mm: float,
) -> cq.Workplane:
    if flange_mm <= 0.05:
        return male
    expanded = bottom.buffer(flange_mm, join_style=1, resolution=16)
    if expanded.is_empty:
        return male
    if expanded.geom_type == "MultiPolygon":
        expanded = max(expanded.geoms, key=lambda g: g.area)
    ring = expanded.difference(bottom)
    if ring.is_empty:
        # Full plate under the mold.
        outer = _clean_ring(expanded.exterior.coords)
        try:
            plate = (
                cq.Workplane("XY")
                .polyline(outer)
                .close()
                .extrude(_FLANGE_THICKNESS_MM)
                .translate((0, 0, -_FLANGE_THICKNESS_MM))
            )
            return male.union(plate)
        except Exception:
            return male

    parts: list[cq.Workplane] = []
    geoms = [ring] if ring.geom_type == "Polygon" else list(getattr(ring, "geoms", []))
    for geom in geoms:
        if geom.geom_type != "Polygon" or geom.is_empty:
            continue
        outer = _clean_ring(geom.exterior.coords)
        if len(outer) < 3:
            continue
        try:
            wp = cq.Workplane("XY").polyline(outer).close()
            for interior in geom.interiors:
                hole = _clean_ring(interior.coords)
                if len(hole) >= 3:
                    wp = wp.polyline(hole).close()
            part = wp.extrude(_FLANGE_THICKNESS_MM).translate(
                (0, 0, -_FLANGE_THICKNESS_MM)
            )
            parts.append(part)
        except Exception:
            continue

    if not parts:
        return male
    flange = parts[0]
    for part in parts[1:]:
        try:
            flange = flange.union(part)
        except Exception:
            continue
    try:
        return male.union(flange)
    except Exception:
        return male


def build_valet_mold(settings: ValetMoldSettings) -> cq.Workplane:
    """Build the male valet-tray wet-form plug."""
    _validate_settings(settings)
    bottom = _bottom_outline(settings)

    if settings.shape == SHAPE_SILHOUETTE:
        outer = _clean_ring(bottom.exterior.coords)
        male = _extrude_tapered_wire(outer, settings.height_mm, settings.draft_deg)
    else:
        male = _extrude_parametric_male(settings)

    male = _apply_top_fillet(male, settings.top_fillet_mm)
    male = _add_flange(male, bottom, settings.flange_mm)
    return male


def build_valet_female(
    settings: ValetMoldSettings,
    bottom: Polygon | None = None,
) -> cq.Workplane:
    """Build a female collar plate: opening ≈ male outline + leather thickness."""
    _validate_settings(settings)
    if bottom is None:
        bottom = _bottom_outline(settings)

    clearance = settings.leather_thickness_mm
    opening = bottom.buffer(clearance, join_style=1, resolution=16)
    if opening.is_empty:
        raise StampGenerationError("Could not build female opening.")
    if opening.geom_type == "MultiPolygon":
        opening = max(opening.geoms, key=lambda g: g.area)

    minx, miny, maxx, maxy = opening.bounds
    wall = _FEMALE_WALL_MM
    outer = Polygon(
        [
            (minx - wall, miny - wall),
            (maxx + wall, miny - wall),
            (maxx + wall, maxy + wall),
            (minx - wall, maxy + wall),
        ]
    )

    plate_poly = outer.difference(opening)
    if plate_poly.is_empty:
        raise StampGenerationError("Female collar opening consumed the plate.")

    geoms = (
        [plate_poly]
        if plate_poly.geom_type == "Polygon"
        else [g for g in plate_poly.geoms if g.geom_type == "Polygon"]
    )
    if not geoms:
        raise StampGenerationError("Female collar produced no geometry.")

    parts: list[cq.Workplane] = []
    for geom in geoms:
        outer_ring = _clean_ring(geom.exterior.coords)
        if len(outer_ring) < 3:
            continue
        try:
            wp = cq.Workplane("XY").polyline(outer_ring).close()
            for interior in geom.interiors:
                hole = _clean_ring(interior.coords)
                if len(hole) >= 3:
                    wp = wp.polyline(hole).close()
            parts.append(wp.extrude(_FEMALE_THICKNESS_MM))
        except Exception:
            continue

    if not parts:
        raise StampGenerationError("Could not extrude female collar.")

    female = parts[0]
    for part in parts[1:]:
        try:
            female = female.union(part)
        except Exception:
            continue
    return female


def build_valet_mold_set(settings: ValetMoldSettings) -> ValetMoldResult:
    """Build male plug and optionally the matching female collar."""
    _validate_settings(settings)
    bottom = _bottom_outline(settings)

    if settings.shape == SHAPE_SILHOUETTE:
        outer = _clean_ring(bottom.exterior.coords)
        male = _extrude_tapered_wire(outer, settings.height_mm, settings.draft_deg)
    else:
        male = _extrude_parametric_male(settings)

    male = _apply_top_fillet(male, settings.top_fillet_mm)
    male = _add_flange(male, bottom, settings.flange_mm)

    female = None
    if settings.include_female:
        female = build_valet_female(settings, bottom=bottom)

    return ValetMoldResult(male=male, female=female)
