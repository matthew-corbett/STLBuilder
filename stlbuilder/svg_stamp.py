"""Parse SVG vector artwork into Shapely polygons for stamp extrusion."""

from __future__ import annotations

import math
from pathlib import Path as FilePath

import cv2
import numpy as np
from matplotlib.font_manager import FontProperties
from matplotlib.path import Path as MplPath
from matplotlib.textpath import TextPath
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
    Circle,
    Ellipse,
)

from stlbuilder.stamp_generator import StampGenerationError

# Curve discretization: aim for short edge lengths so arcs stay smooth on the stamp.
_MIN_SAMPLES_PER_CURVE = 64
_MAX_SAMPLES_PER_CURVE = 480
_MIN_AREA = 1e-8


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


def _step_for_svg(diagonal: float) -> float:
    """SVG-unit step length targeting ~0.3 mm chords on a ~50 mm-wide stamp."""
    if diagonal <= 0:
        return 0.12
    return max(0.04, min(0.4, diagonal * 0.0005))


def _curve_sample_count(seg, step: float) -> int:
    try:
        length = float(seg.length())
    except Exception:
        length = 20.0
    if length <= 0:
        return _MIN_SAMPLES_PER_CURVE
    n = int(math.ceil(length / max(step, 1e-6)))
    return max(_MIN_SAMPLES_PER_CURVE, min(_MAX_SAMPLES_PER_CURVE, n))


def _sample_path_rings(
    path: SvgPath, step: float = 0.1
) -> list[list[tuple[float, float]]]:
    """Convert a Path into closed rings with dense arc-length sampling."""
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
            n = _curve_sample_count(seg, step)
            for i in range(1, n + 1):
                p = seg.point(i / n)
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
    """Non-background fills raise on the stamp; near-white punches holes."""
    fill = getattr(element, "fill", None)
    if _is_paint_none(fill):
        return False
    if not isinstance(fill, Color):
        return True
    try:
        r, g, b = float(fill.red), float(fill.green), float(fill.blue)
    except Exception:
        return True
    # Only near-white is treated as background (keeps gold/yellow/gray text).
    return not (r >= 245 and g >= 245 and b >= 245)


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


def _font_family_name(element: Text) -> str:
    family = getattr(element, "font_family", None) or "Arial"
    if isinstance(family, (list, tuple)):
        family = family[0] if family else "Arial"
    family = str(family)
    if "," in family:
        family = family.split(",")[0]
    return family.strip().strip("'\"") or "Arial"


def _mpl_path_to_rings(path: MplPath) -> list[list[tuple[float, float]]]:
    """Convert a matplotlib path into closed coordinate rings."""
    rings: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    verts = path.vertices
    codes = path.codes
    if codes is None:
        if len(verts) >= 3:
            rings.append([(float(x), float(y)) for x, y in verts])
        return rings

    i = 0
    while i < len(codes):
        code = codes[i]
        if code == MplPath.MOVETO:
            if len(current) >= 3:
                rings.append(current)
            current = [(float(verts[i][0]), float(verts[i][1]))]
            i += 1
        elif code == MplPath.LINETO:
            current.append((float(verts[i][0]), float(verts[i][1])))
            i += 1
        elif code == MplPath.CURVE3:
            p0 = current[-1] if current else (float(verts[i][0]), float(verts[i][1]))
            p1 = (float(verts[i][0]), float(verts[i][1]))
            p2 = (float(verts[i + 1][0]), float(verts[i + 1][1]))
            for t in range(1, 25):
                u = t / 24.0
                x = (1 - u) ** 2 * p0[0] + 2 * (1 - u) * u * p1[0] + u**2 * p2[0]
                y = (1 - u) ** 2 * p0[1] + 2 * (1 - u) * u * p1[1] + u**2 * p2[1]
                current.append((x, y))
            i += 2
        elif code == MplPath.CURVE4:
            p0 = current[-1] if current else (float(verts[i][0]), float(verts[i][1]))
            p1 = (float(verts[i][0]), float(verts[i][1]))
            p2 = (float(verts[i + 1][0]), float(verts[i + 1][1]))
            p3 = (float(verts[i + 2][0]), float(verts[i + 2][1]))
            for t in range(1, 33):
                u = t / 32.0
                x = (
                    (1 - u) ** 3 * p0[0]
                    + 3 * (1 - u) ** 2 * u * p1[0]
                    + 3 * (1 - u) * u**2 * p2[0]
                    + u**3 * p3[0]
                )
                y = (
                    (1 - u) ** 3 * p0[1]
                    + 3 * (1 - u) ** 2 * u * p1[1]
                    + 3 * (1 - u) * u**2 * p2[1]
                    + u**3 * p3[1]
                )
                current.append((x, y))
            i += 3
        elif code == MplPath.CLOSEPOLY:
            if len(current) >= 3:
                rings.append(current)
            current = []
            i += 1
        else:
            i += 1

    if len(current) >= 3:
        rings.append(current)
    return rings


def _text_to_polygons(element: Text) -> list[Polygon]:
    """Outline live SVG <text> using a system font (matplotlib TextPath)."""
    content = element.text
    if content is None:
        return []
    content = str(content)
    if not content.strip():
        return []

    try:
        size = float(element.font_size or 12.0)
    except (TypeError, ValueError):
        size = 12.0
    if size <= 0:
        return []

    family = _font_family_name(element)
    weight = (
        "bold"
        if str(getattr(element, "font_weight", "")).lower()
        in ("bold", "bolder", "700", "800", "900")
        else "normal"
    )
    style = (
        "italic"
        if str(getattr(element, "font_style", "")).lower() in ("italic", "oblique")
        else "normal"
    )

    try:
        prop = FontProperties(family=family, size=size, weight=weight, style=style)
        # Size is in SVG user units so glyph outlines match font-size.
        tp = TextPath((0, 0), content, size=size, prop=prop, usetex=False)
    except Exception:
        return []

    rings = _mpl_path_to_rings(tp)
    if not rings:
        return []

    try:
        origin_x = float(element.x or 0.0)
        origin_y = float(element.y or 0.0)
    except (TypeError, ValueError):
        origin_x, origin_y = 0.0, 0.0

    transform = getattr(element, "transform", None)
    placed: list[list[tuple[float, float]]] = []
    for ring in rings:
        pts: list[tuple[float, float]] = []
        for mx, my in ring:
            # Matplotlib Y-up → SVG Y-down around the text baseline.
            sx = origin_x + mx
            sy = origin_y - my
            if transform is not None:
                try:
                    pt = transform.point_in_matrix_space(sx, sy)
                    sx, sy = float(pt.x), float(pt.y)
                except Exception:
                    pass
            pts.append((sx, sy))
        placed.append(pts)

    polys = [_ring_to_polygon(r) for r in placed]
    polys = [p for p in polys if p is not None]
    return _nest_polygons(polys)


def _svg_diagonal(svg: SVG) -> float:
    try:
        bbox = svg.bbox()
        if bbox is None:
            return 100.0
        minx, miny, maxx, maxy = bbox
        diagonal = ((maxx - minx) ** 2 + (maxy - miny) ** 2) ** 0.5
        return diagonal if diagonal > 0 else 100.0
    except Exception:
        return 100.0


def _circle_ellipse_rings(element: Shape, step: float) -> list[list[tuple[float, float]]] | None:
    """Dense angular sampling for Circle/Ellipse (avoids coarse Arc approximation)."""
    if isinstance(element, Circle):
        try:
            cx, cy = float(element.cx), float(element.cy)
            r = float(getattr(element, "r", None) or element.rx)
        except Exception:
            return None
        if r <= 0:
            return None
        circumference = 2.0 * math.pi * r
        n = max(96, min(480, int(math.ceil(circumference / max(step, 1e-6)))))
        ring = []
        for i in range(n):
            a = 2.0 * math.pi * i / n
            x = cx + r * math.cos(a)
            y = cy + r * math.sin(a)
            transform = getattr(element, "transform", None)
            if transform is not None:
                try:
                    pt = transform.point_in_matrix_space(x, y)
                    x, y = float(pt.x), float(pt.y)
                except Exception:
                    pass
            ring.append((x, y))
        return [ring]

    if isinstance(element, Ellipse):
        try:
            cx, cy = float(element.cx), float(element.cy)
            rx, ry = float(element.rx), float(element.ry)
        except Exception:
            return None
        if rx <= 0 or ry <= 0:
            return None
        # Ramanujan approximation for perimeter.
        h = ((rx - ry) ** 2) / ((rx + ry) ** 2) if (rx + ry) else 0.0
        circumference = math.pi * (rx + ry) * (1.0 + 3.0 * h / (10.0 + math.sqrt(max(0.0, 4.0 - 3.0 * h))))
        n = max(96, min(480, int(math.ceil(circumference / max(step, 1e-6)))))
        ring = []
        for i in range(n):
            a = 2.0 * math.pi * i / n
            x = cx + rx * math.cos(a)
            y = cy + ry * math.sin(a)
            transform = getattr(element, "transform", None)
            if transform is not None:
                try:
                    pt = transform.point_in_matrix_space(x, y)
                    x, y = float(pt.x), float(pt.y)
                except Exception:
                    pass
            ring.append((x, y))
        return [ring]

    return None


def _element_to_geometry(element: Shape, step: float = 0.1):
    """Return Shapely geometry for a shape or text element, or None."""
    if isinstance(element, Text):
        polys = _text_to_polygons(element)
        if not polys:
            return None
        return unary_union(polys)

    has_fill = _shape_has_fill(element)
    has_stroke = _shape_has_stroke(element)
    if not has_fill and not has_stroke:
        return None

    rings = _circle_ellipse_rings(element, step)
    if rings is None:
        try:
            path_geom = SvgPath(element)
            path_geom.reify()
        except Exception:
            return None
        rings = _sample_path_rings(path_geom, step=step)

    if not rings:
        return None

    parts: list[Polygon] = []
    if has_fill:
        shape_polys = [_ring_to_polygon(r) for r in rings]
        shape_polys = [p for p in shape_polys if p is not None]
        if shape_polys:
            parts.extend(_nest_polygons(shape_polys))
    elif has_stroke:
        try:
            stroke_w = float(element.stroke_width or 1.0)
        except (TypeError, ValueError):
            stroke_w = 1.0
        parts.extend(_stroke_rings_to_polygons(rings, stroke_w))

    if not parts:
        return None
    return unary_union(parts)


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

    step = _step_for_svg(_svg_diagonal(svg))
    composed = None  # paint-order boolean composition
    skipped_text = 0

    for element in svg.elements():
        if isinstance(element, (Group, Image, Use, SVG)):
            continue
        if isinstance(element, Text):
            shape_geom = _element_to_geometry(element, step=step)
            if shape_geom is None or shape_geom.is_empty:
                skipped_text += 1
                continue
            if _fill_is_raised(element) or _shape_has_stroke(element):
                composed = shape_geom if composed is None else composed.union(shape_geom)
            elif composed is not None:
                composed = composed.difference(shape_geom)
            continue

        if not isinstance(element, Shape):
            continue

        shape_geom = _element_to_geometry(element, step=step)
        if shape_geom is None or shape_geom.is_empty:
            continue

        has_fill = _shape_has_fill(element)
        if has_fill and not _fill_is_raised(element):
            if composed is None:
                continue
            composed = composed.difference(shape_geom)
        else:
            composed = shape_geom if composed is None else composed.union(shape_geom)

    if composed is None or composed.is_empty:
        hint = ""
        if skipped_text:
            hint = (
                f" ({skipped_text} text element(s) could not be outlined — "
                "install the SVG's font or convert text to paths)."
            )
        raise StampGenerationError(
            "No filled shapes found in SVG. Use filled paths "
            "or convert text to outlines before importing." + hint
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
