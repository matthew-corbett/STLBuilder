"""Parse SVG vector artwork into Shapely polygons for stamp extrusion."""

from __future__ import annotations

from pathlib import Path as FilePath

import cv2
import numpy as np
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union
from svgelements import (
    SVG,
    Color,
    Path as SvgPath,
    Shape,
    Group,
    Text,
    Image,
    Use,
    Move,
    Close,
)

from stlbuilder.stamp_generator import StampGenerationError

# denser sampling = smoother curves, heavier CAD
_CURVE_SAMPLES = 32
_MIN_AREA = 1e-6


def is_svg_path(path: str | FilePath) -> bool:
    return FilePath(path).suffix.lower() == ".svg"


def _is_paint_none(paint) -> bool:
    if paint is None:
        return True
    if isinstance(paint, str) and paint.strip().lower() in ("none", ""):
        return True
    if isinstance(paint, Color):
        # svgelements uses Color('None') with value None for fill="none"
        if paint.value is None:
            return True
        hexval = getattr(paint, "hex", None)
        if hexval in (None, "", "none"):
            return True
    return False


def _sample_path_rings(path: SvgPath) -> list[list[tuple[float, float]]]:
    """Convert a Path into closed rings by sampling each subpath."""
    rings: list[list[tuple[float, float]]] = []
    for subpath in path.as_subpaths():
        if not subpath:
            continue
        pts: list[tuple[float, float]] = []
        for seg in subpath:
            if isinstance(seg, Move):
                pts.append((float(seg.end.x), float(seg.end.y)))
                continue
            if isinstance(seg, Close):
                if pts and pts[0] != pts[-1]:
                    pts.append(pts[0])
                continue
            start = seg.start
            if not pts:
                pts.append((float(start.x), float(start.y)))
            # Straight segments need only the end point.
            if type(seg).__name__ == "Line":
                pts.append((float(seg.end.x), float(seg.end.y)))
                continue
            for i in range(1, _CURVE_SAMPLES + 1):
                p = seg.point(i / _CURVE_SAMPLES)
                pts.append((float(p.x), float(p.y)))

        cleaned: list[tuple[float, float]] = []
        for pt in pts:
            if not cleaned or (
                abs(cleaned[-1][0] - pt[0]) > 1e-9 or abs(cleaned[-1][1] - pt[1]) > 1e-9
            ):
                cleaned.append(pt)
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1]:
            cleaned = cleaned[:-1]
        if len(cleaned) >= 3:
            rings.append(cleaned)
    return rings


def _ring_to_polygon(ring: list[tuple[float, float]]) -> Polygon | None:
    try:
        poly = Polygon(ring)
    except ValueError:
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return None
    if poly.geom_type == "MultiPolygon":
        parts = [g for g in poly.geoms if g.geom_type == "Polygon" and not g.is_empty]
        if not parts:
            return None
        poly = max(parts, key=lambda g: g.area)
    if poly.geom_type != "Polygon" or poly.area < _MIN_AREA:
        return None
    return poly


def _nest_polygons(polys: list[Polygon]) -> list[Polygon]:
    """Turn nested rings into exteriors with holes."""
    if not polys:
        return []

    ordered = sorted(polys, key=lambda p: p.area, reverse=True)
    used = [False] * len(ordered)
    result: list[Polygon] = []

    for i, outer in enumerate(ordered):
        if used[i]:
            continue
        holes: list = []
        for j, inner in enumerate(ordered):
            if j <= i or used[j]:
                continue
            if outer.contains(inner) or (
                outer.covers(inner) and inner.centroid.within(outer)
            ):
                inside_other_hole = False
                for other in holes:
                    if Polygon(other).contains(inner):
                        inside_other_hole = True
                        break
                if inside_other_hole:
                    continue
                holes.append(list(inner.exterior.coords))
                used[j] = True
        try:
            poly = Polygon(outer.exterior.coords, holes)
        except ValueError:
            poly = outer
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            continue
        if poly.geom_type == "Polygon" and poly.area >= _MIN_AREA:
            result.append(poly)
            used[i] = True
        elif poly.geom_type == "MultiPolygon":
            for g in poly.geoms:
                if g.geom_type == "Polygon" and g.area >= _MIN_AREA:
                    result.append(g)
            used[i] = True

    return result


def _shape_has_fill(element: Shape) -> bool:
    return not _is_paint_none(getattr(element, "fill", None))


def _shape_has_stroke(element: Shape) -> bool:
    if _is_paint_none(getattr(element, "stroke", None)):
        return False
    width = getattr(element, "stroke_width", None)
    if width is None:
        return True
    try:
        return float(width) > 0
    except (TypeError, ValueError):
        return True


def _fill_is_raised(element: Shape) -> bool:
    """Dark fills raise on the stamp; light/white fills cut holes (paint order)."""
    fill = getattr(element, "fill", None)
    if _is_paint_none(fill):
        return False
    if not isinstance(fill, Color):
        return True
    # Luminance relative to white: cut if mostly light.
    try:
        r, g, b = fill.red, fill.green, fill.blue
        # svgelements channels are 0-255
        luma = (0.299 * float(r) + 0.587 * float(g) + 0.114 * float(b)) / 255.0
    except Exception:
        return True
    return luma < 0.85


def _geometry_to_polygon_list(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom] if geom.area >= _MIN_AREA else []
    if geom.geom_type == "MultiPolygon":
        return [g for g in geom.geoms if g.area >= _MIN_AREA]
    if geom.geom_type == "GeometryCollection":
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(_geometry_to_polygon_list(g))
        return out
    return []


def _stroke_rings_to_polygons(
    rings: list[list[tuple[float, float]]], stroke_width: float
) -> list[Polygon]:
    half = max(stroke_width, 0.1) / 2.0
    out: list[Polygon] = []
    for ring in rings:
        if len(ring) < 2:
            continue
        line_coords = list(ring)
        if len(line_coords) >= 3:
            dist = (
                (line_coords[0][0] - line_coords[-1][0]) ** 2
                + (line_coords[0][1] - line_coords[-1][1]) ** 2
            ) ** 0.5
            if dist < 1e-6:
                line_coords = line_coords[:-1] + [line_coords[0]]
        buffered = LineString(line_coords).buffer(half, cap_style=2, join_style=2)
        out.extend(_geometry_to_polygon_list(buffered))
    return out


def svg_to_polygons(path: str | FilePath) -> tuple[list[Polygon], float, float]:
    """Load an SVG and return (polygons in SVG coords, width, height).

    Coordinates use SVG Y-down. Caller should flip Y for CAD.
    Width/height come from the content bounding box.
    """
    src = FilePath(path)
    try:
        svg = SVG.parse(str(src))
    except Exception as exc:
        raise StampGenerationError("Could not parse SVG file.") from exc

    flat_polys: list[Polygon] = []
    composed = None  # paint-order boolean composition

    for element in svg.elements():
        if isinstance(element, (Group, Text, Image, Use, SVG)):
            continue
        if not isinstance(element, Shape):
            continue

        has_fill = _shape_has_fill(element)
        has_stroke = _shape_has_stroke(element)
        if not has_fill and not has_stroke:
            continue

        try:
            path_geom = SvgPath(element)
            path_geom.reify()
        except Exception:
            continue

        rings = _sample_path_rings(path_geom)
        if not rings:
            continue

        shape_geom = None
        if has_fill:
            shape_polys = [_ring_to_polygon(r) for r in rings]
            shape_polys = [p for p in shape_polys if p is not None]
            if shape_polys:
                shape_geom = unary_union(_nest_polygons(shape_polys))
        elif has_stroke:
            try:
                stroke_w = float(element.stroke_width or 1.0)
            except (TypeError, ValueError):
                stroke_w = 1.0
            stroked = _stroke_rings_to_polygons(rings, stroke_w)
            if stroked:
                shape_geom = unary_union(stroked)

        if shape_geom is None or shape_geom.is_empty:
            continue

        if has_fill and not _fill_is_raised(element):
            # Light fill punches a hole through earlier dark shapes.
            if composed is None:
                continue
            composed = composed.difference(shape_geom)
        else:
            composed = shape_geom if composed is None else composed.union(shape_geom)

    if composed is None or composed.is_empty:
        raise StampGenerationError(
            "No filled shapes found in SVG. Use filled paths (not text-only) "
            "or convert text to outlines before importing."
        )

    polygons = _geometry_to_polygon_list(composed)
    if not polygons:
        raise StampGenerationError("SVG produced no usable geometry.")

    merged = unary_union(polygons)
    minx, miny, maxx, maxy = merged.bounds
    width = max(maxx - minx, 1e-6)
    height = max(maxy - miny, 1e-6)

    shifted: list[Polygon] = []
    for poly in polygons:
        shifted.append(
            Polygon(
                [(x - minx, y - miny) for x, y in poly.exterior.coords],
                [
                    [(x - minx, y - miny) for x, y in hole.coords]
                    for hole in poly.interiors
                ],
            )
        )

    return shifted, width, height


def flip_y_polygons(polygons: list[Polygon], height: float) -> list[Polygon]:
    """Convert SVG Y-down coordinates to CAD Y-up."""
    flipped: list[Polygon] = []
    for poly in polygons:
        flipped.append(
            Polygon(
                [(x, height - y) for x, y in poly.exterior.coords],
                [[(x, height - y) for x, y in hole.coords] for hole in poly.interiors],
            )
        )
    return flipped


def scale_polygons_to_width(
    polygons: list[Polygon], src_width: float, src_height: float, width_mm: float
) -> tuple[list[Polygon], float]:
    """Scale SVG-space polygons to mm and center on origin. Returns (polys, height_mm)."""
    if src_width <= 0:
        raise StampGenerationError("SVG width is invalid.")
    scale = width_mm / src_width
    height_mm = src_height * scale
    offset_x = -width_mm / 2
    offset_y = -height_mm / 2
    scaled: list[Polygon] = []
    for poly in polygons:
        scaled.append(
            Polygon(
                [
                    (x * scale + offset_x, y * scale + offset_y)
                    for x, y in poly.exterior.coords
                ],
                [
                    [
                        (x * scale + offset_x, y * scale + offset_y)
                        for x, y in hole.coords
                    ]
                    for hole in poly.interiors
                ],
            )
        )
    return scaled, height_mm


def invert_polygons(
    polygons: list[Polygon], width: float, height: float
) -> list[Polygon]:
    """Raise the background instead of the filled shapes (within content bounds)."""
    frame = box(0, 0, width, height)
    filled = unary_union(polygons)
    inverted = frame.difference(filled)
    return _geometry_to_polygon_list(inverted)


def rasterize_polygons(
    polygons: list[Polygon],
    width: float,
    height: float,
    max_pixels: int = 800,
) -> np.ndarray:
    """Render polygons to a binary mask for the 2D UI preview (Y-down)."""
    if width <= 0 or height <= 0:
        return np.zeros((1, 1), dtype=np.uint8)

    scale = max_pixels / max(width, height)
    w_px = max(1, int(round(width * scale)))
    h_px = max(1, int(round(height * scale)))
    mask = np.zeros((h_px, w_px), dtype=np.uint8)

    def to_px(coords):
        pts = []
        for x, y in coords:
            pts.append([int(round(x * scale)), int(round(y * scale))])
        return np.array(pts, dtype=np.int32)

    for poly in polygons:
        outer = to_px(list(poly.exterior.coords))
        if len(outer) >= 3:
            cv2.fillPoly(mask, [outer], 255)
        for hole in poly.interiors:
            hole_pts = to_px(list(hole.coords))
            if len(hole_pts) >= 3:
                cv2.fillPoly(mask, [hole_pts], 0)

    return mask
