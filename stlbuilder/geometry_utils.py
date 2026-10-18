"""Shared helpers for building stamp geometry."""

from __future__ import annotations

import math

import cadquery as cq

BASE_SHAPE_RECT = "rect"
BASE_SHAPE_ROUND = "round"
VALID_BASE_SHAPES = (BASE_SHAPE_RECT, BASE_SHAPE_ROUND)


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


def build_stamp_base(
    bb: cq.BoundBox,
    *,
    base_thickness: float,
    base_shape: str = BASE_SHAPE_RECT,
    margin: float = 4.0,
    diameter_mm: float | None = None,
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

        return (
            cq.Workplane("XY")
            .circle(diameter / 2.0)
            .extrude(base_thickness)
            .translate((0, 0, -base_thickness))
        )

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
