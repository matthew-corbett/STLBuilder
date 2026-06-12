"""Shared helpers for building stamp geometry."""

from __future__ import annotations

import cadquery as cq


def build_base_plate(
    bb: cq.BoundBox,
    margin: float,
    base_thickness: float,
) -> cq.Workplane:
    """Create a base plate sized to bounds + margin (top face at Z=0)."""
    return (
        cq.Workplane("XY")
        .box(
            bb.xlen + 2 * margin,
            bb.ylen + 2 * margin,
            base_thickness,
        )
        .translate((bb.center.x, bb.center.y, -base_thickness / 2))
    )


def apply_mirror(model: cq.Workplane, mirror: bool) -> cq.Workplane:
    if mirror:
        return model.mirror(mirrorPlane="YZ")
    return model


def union_solids(*parts: cq.Workplane) -> cq.Workplane:
    result = parts[0]
    for part in parts[1:]:
        result = result.union(part)
    return result
