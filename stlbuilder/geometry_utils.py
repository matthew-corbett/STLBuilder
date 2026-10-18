"""Shared helpers for building stamp geometry."""

from __future__ import annotations

import math

import cadquery as cq

BASE_SHAPE_RECT = "rect"
BASE_SHAPE_ROUND = "round"
VALID_BASE_SHAPES = (BASE_SHAPE_RECT, BASE_SHAPE_ROUND)

# Image artwork centering: bounds = AABB center (seals/logos);
# centroid = area-weighted center (pointed shields).
CENTER_BOUNDS = "bounds"
CENTER_CENTROID = "centroid"
VALID_CENTER_MODES = (CENTER_BOUNDS, CENTER_CENTROID)


def build_base_plate(
    bb: cq.BoundBox,
    margin: float,
    base_thickness: float,
) -> cq.Workplane:
    """Create a rectangular base plate sized to bounds + margin (top face at Z=0)."""
    return build_stamp_base(
        bb,
        base_thickness=base_thickness,
        base_shape=BASE_SHAPE_RECT,
        margin=margin,
    )


def _max_radius_from_origin(bb: cq.BoundBox) -> float:
    """Largest distance from origin to a corner of the XY bounding box."""
    return max(
        math.hypot(bb.xmin, bb.ymin),
        math.hypot(bb.xmin, bb.ymax),
        math.hypot(bb.xmax, bb.ymin),
        math.hypot(bb.xmax, bb.ymax),
    )


def _back_orientation_tick(
    diameter_mm: float,
    base_thickness: float,
    *,
    tick_length_mm: float = 4.0,
    tick_width_mm: float = 2.5,
    tick_height_mm: float = 1.2,
    edge_inset_mm: float = 1.5,
) -> cq.Workplane:
    """Raised tick on the stamp back (+Y) so you can feel which way is up.

    Stamp face is at Z=0 (design +Z); base fills Z in [-base_thickness, 0].
    The tick sits on the back face and protrudes further in -Z, near the +Y rim
    (top of the design).
    """
    radius = diameter_mm / 2.0
    length = min(tick_length_mm, max(2.0, radius * 0.35))
    width = min(tick_width_mm, max(1.5, radius * 0.2))
    height = min(tick_height_mm, max(0.6, base_thickness * 0.35))
    inset = max(0.8, edge_inset_mm)
    # Center of the tick, inset from the +Y rim toward the origin.
    y = radius - inset - length / 2.0
    if y < length / 2.0:
        y = length / 2.0
    return (
        cq.Workplane("XY")
        .box(width, length, height)
        .translate((0.0, y, -base_thickness - height / 2.0))
    )


def build_stamp_base(
    bb: cq.BoundBox,
    *,
    base_thickness: float,
    base_shape: str = BASE_SHAPE_RECT,
    margin: float = 4.0,
    diameter_mm: float | None = None,
    back_tick: bool = False,
) -> cq.Workplane:
    """Create a stamp base with top face at Z=0, always centered on the origin.

    Relief artwork is expected to already be positioned about (0, 0). The base
    is sized to cover the relief extents (plus margin) without shifting the
    visual center of asymmetric logos.
    """
    shape = (base_shape or BASE_SHAPE_RECT).lower()
    if shape not in VALID_BASE_SHAPES:
        raise ValueError(f"Unknown base shape '{base_shape}'. Use rect or round.")
    if base_thickness <= 0:
        raise ValueError("Base thickness must be greater than zero.")

    if shape == BASE_SHAPE_ROUND:
        if diameter_mm is not None and diameter_mm > 0:
            diameter = float(diameter_mm)
        else:
            diameter = 2.0 * _max_radius_from_origin(bb) + 2 * max(0.0, margin)
        if diameter <= 0:
            raise ValueError("Stamp diameter must be greater than zero.")

        # Axis extents only — AABB corners of pointed logos (shields) sit outside
        # the real silhouette and would falsely fail a corner-radius check.
        half_extent = max(
            abs(bb.xmin), abs(bb.xmax), abs(bb.ymin), abs(bb.ymax)
        )
        if half_extent > diameter / 2.0 + 0.05:
            raise ValueError(
                f"Design is too large for a {diameter:.1f} mm round stamp "
                f"(extends about {2.0 * half_extent:.1f} mm). "
                "Increase diameter, reduce size, or add less margin."
            )

        base = (
            cq.Workplane("XY")
            .circle(diameter / 2.0)
            .extrude(base_thickness)
            .translate((0, 0, -base_thickness))
        )
        if back_tick:
            try:
                base = base.union(
                    _back_orientation_tick(diameter, base_thickness)
                )
            except Exception:
                pass
        return base

    if margin < 0:
        raise ValueError("Margin cannot be negative.")
    half_x = max(abs(bb.xmin), abs(bb.xmax))
    half_y = max(abs(bb.ymin), abs(bb.ymax))
    return (
        cq.Workplane("XY")
        .box(
            2.0 * half_x + 2 * margin,
            2.0 * half_y + 2 * margin,
            base_thickness,
        )
        .translate((0, 0, -base_thickness / 2))
    )


def round_content_width_mm(
    diameter_mm: float,
    margin: float,
    aspect_h_over_w: float = 1.0,
) -> float:
    """Max artwork width that fits a circle of diameter_mm with edge inset margin.

    Uses the bounding-box diagonal so rectangular artwork stays inside the disk.
    Prefer circumradius fitting via scale_polygons for round stamps when possible.
    """
    usable = max(1.0, float(diameter_mm) - 2.0 * max(0.0, float(margin)))
    aspect = max(1e-6, abs(float(aspect_h_over_w)))
    return usable / math.hypot(1.0, aspect)


def apply_mirror(model: cq.Workplane, mirror: bool) -> cq.Workplane:
    if mirror:
        return model.mirror(mirrorPlane="YZ")
    return model


def union_solids(*parts: cq.Workplane) -> cq.Workplane:
    result = parts[0]
    for part in parts[1:]:
        result = result.union(part)
    return result
