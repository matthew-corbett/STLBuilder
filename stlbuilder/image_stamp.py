"""Convert imported images into raised-relief stamp geometry."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import cadquery as cq
import cv2
import numpy as np
from PIL import Image
from shapely.geometry import Polygon
from shapely.ops import unary_union

from stlbuilder.geometry_utils import apply_mirror, build_base_plate
from stlbuilder.stamp_generator import StampGenerationError
from stlbuilder.svg_stamp import (
    flip_y_polygons,
    invert_polygons,
    is_svg_path,
    rasterize_polygons,
    scale_polygons_to_width,
    svg_to_polygons,
)


@dataclass
class ImageStampSettings:
    image_path: str
    width_mm: float = 40.0
    imprint_depth: float = 2.0
    base_thickness: float = 5.0
    margin: float = 4.0
    mirror_for_leather: bool = True
    threshold: int = 128
    invert: bool = False
    simplify: float = 0.0
    max_pixels: int = 3200
    raised_border: bool = False
    border_width: float = 1.5
    # Soften pixel stair-steps on raster silhouettes before contouring (odd px, 0=off).
    edge_smooth_px: int = 3
    # Unused for circular rings (analytic). Kept for API compatibility.
    curve_smooth_passes: int = 0
    # When downsampling dense rings, aim for this chord length (mm).
    max_edge_mm: float = 0.22
    # Hard cap per ring after downsample (source contours stay denser until then).
    max_ring_points: int = 360
    # Letter/monogram: light Chaikin, then bounded chords (keep CadQuery fast).
    letter_smooth_passes: int = 2
    letter_max_edge_mm: float = 0.12
    letter_max_points: int = 400
    # Absolute CadQuery budget: exterior + all holes combined.
    max_total_vertices: int = 2200
    # Drop holes smaller than this (mm^2) — invisible on FDM, expensive to boolean.
    min_hole_area_mm2: float = 0.20


def _validate_settings(settings: ImageStampSettings) -> None:
    path = Path(settings.image_path)
    if not path.is_file():
        raise StampGenerationError(f"Image not found: {settings.image_path}")
    if settings.width_mm <= 0:
        raise StampGenerationError("Stamp width must be greater than zero.")
    if settings.imprint_depth <= 0:
        raise StampGenerationError("Imprint depth must be greater than zero.")
    if settings.base_thickness <= 0:
        raise StampGenerationError("Base thickness must be greater than zero.")
    if settings.margin < 0:
        raise StampGenerationError("Margin cannot be negative.")
    if not 0 <= settings.threshold <= 255:
        raise StampGenerationError("Threshold must be between 0 and 255.")
    if settings.raised_border and settings.border_width <= 0:
        raise StampGenerationError("Border width must be greater than zero.")


def _load_grayscale_array(path: str, max_pixels: int) -> np.ndarray:
    try:
        img = Image.open(path)
    except Exception as exc:
        raise StampGenerationError("Could not open image file.") from exc

    img = img.convert("RGBA")
    background = Image.new("RGBA", img.size, (255, 255, 255, 255))
    background.paste(img, mask=img.split()[3])
    gray = np.array(background.convert("L"), dtype=np.uint8)

    height, width = gray.shape
    longest = max(height, width)
    if longest <= 0:
        return gray

    # Always normalize to max_pixels on the long edge. Small logos (e.g. 480px)
    # must be upscaled or letter curves stay jagged pixel stairs; rings can still
    # be replaced with analytic circles later.
    target = max(256, int(max_pixels))
    if longest != target:
        scale = target / longest
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
        gray = cv2.resize(gray, (new_w, new_h), interpolation=interp)

    return gray


def _binary_mask(gray: np.ndarray, threshold: int, invert: bool) -> np.ndarray:
    if invert:
        mask = (gray >= threshold).astype(np.uint8) * 255
    else:
        mask = (gray < threshold).astype(np.uint8) * 255
    return mask


def _smooth_mask_edges(mask: np.ndarray, blur_px: int) -> np.ndarray:
    """Blur + re-threshold; optionally supersample first for rounder letter edges."""
    if blur_px is None or blur_px < 2:
        return mask
    # Contour on a 2× grid so pixel stairs become sub-mm after scaling to stamp size.
    h, w = mask.shape
    hi = cv2.resize(mask, (w * 2, h * 2), interpolation=cv2.INTER_LINEAR)
    # Keep blur modest so close double-rings don't open into a wide white gap.
    k = max(3, int(blur_px) * 2 - 1)
    if k % 2 == 0:
        k += 1
    blurred = cv2.GaussianBlur(hi, (k, k), 0)
    return np.where(blurred >= 128, 255, 0).astype(np.uint8)


def _simplify_contour(cnt: np.ndarray, simplify: float) -> np.ndarray:
    """Reduce contour vertices. simplify is percent of perimeter (0 = light auto-cap).

    Keep epsilon small in pixel space — a 2 px cap on a 1600 px seal turns
    outer rings into obvious polygons once scaled to mm.
    Always hard-cap vertex count so CadQuery never sees 10k+ point rings.
    """
    if len(cnt) < 3:
        return cnt

    perimeter = cv2.arcLength(cnt, True)
    if perimeter <= 0:
        return cnt

    if simplify > 0:
        epsilon = (simplify / 100.0) * perimeter
        epsilon = min(max(epsilon, 0.2), 0.45)
        if perimeter < 80:
            epsilon = min(epsilon, perimeter * 0.02)
    else:
        # Tiny denoise for pixel zigzags when Edge simplify is 0.
        epsilon = min(0.35, max(0.15, perimeter * 0.0004))

    approx = cv2.approxPolyDP(cnt, epsilon, True)
    if len(approx) < 3:
        approx = cnt

    max_pts = 1800
    if len(approx) <= max_pts:
        return approx

    lo = float(epsilon)
    hi = max(perimeter * 0.05, lo * 2.0)
    best = approx
    for _ in range(24):
        mid = (lo + hi) * 0.5
        candidate = cv2.approxPolyDP(cnt, mid, True)
        if len(candidate) < 3:
            hi = mid
            continue
        if len(candidate) > max_pts:
            lo = mid
        else:
            best = candidate
            hi = mid
    return best if len(best) >= 3 else approx


def _contours_to_polygons(
    mask: np.ndarray,
    simplify: float,
) -> list[Polygon]:
    """Extract filled regions (with holes) as shapely polygons in pixel coords."""
    # NONE keeps every boundary pixel so curves aren't pre-faceted.
    contours, hierarchy = cv2.findContours(
        mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE
    )
    if hierarchy is None or len(contours) == 0:
        return []

    hierarchy = hierarchy[0]
    outers: dict[int, np.ndarray] = {}
    holes_by_parent: dict[int, list[np.ndarray]] = {}

    for idx, cnt in enumerate(contours):
        if len(cnt) < 3:
            continue
        parent = hierarchy[idx][3]
        approx = _simplify_contour(cnt, simplify)
        if len(approx) < 3:
            continue
        ring = approx.reshape(-1, 2)
        if parent == -1:
            outers[idx] = ring
        else:
            holes_by_parent.setdefault(parent, []).append(ring)

    polygons: list[Polygon] = []
    img_h = mask.shape[0]
    for idx, outer in outers.items():
        holes = holes_by_parent.get(idx, [])
        # Convert image Y (down) to CAD Y (up).
        outer_xy = [(float(x), float(img_h - y)) for x, y in outer]
        hole_xy = [
            [(float(x), float(img_h - y)) for x, y in hole] for hole in holes
        ]
        try:
            poly = Polygon(outer_xy, hole_xy)
        except ValueError:
            continue
        if poly.is_empty:
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)
            if poly.is_empty:
                continue
        geoms: list[Polygon] = []
        if poly.geom_type == "Polygon":
            geoms = [poly]
        elif poly.geom_type == "MultiPolygon":
            geoms = [g for g in poly.geoms if g.geom_type == "Polygon"]
        elif poly.geom_type == "GeometryCollection":
            for g in poly.geoms:
                if g.geom_type == "Polygon":
                    geoms.append(g)
                elif g.geom_type == "MultiPolygon":
                    geoms.extend(p for p in g.geoms if p.geom_type == "Polygon")
        for geom in geoms:
            if not geom.is_empty and geom.area >= 4:
                polygons.append(geom)

    return polygons


def _scale_polygons(
    polygons: list[Polygon], width_mm: float, mask_shape: tuple[int, int]
) -> list[Polygon]:
    img_h, img_w = mask_shape
    if img_w <= 0 or img_h <= 0:
        return []
    scale = width_mm / img_w
    height_mm = img_h * scale
    # Center geometry on origin.
    offset_x = -width_mm / 2
    offset_y = -height_mm / 2
    scaled: list[Polygon] = []
    for poly in polygons:
        scaled.append(
            Polygon(
                [(x * scale + offset_x, y * scale + offset_y) for x, y in poly.exterior.coords],
                [
                    [(x * scale + offset_x, y * scale + offset_y) for x, y in hole.coords]
                    for hole in poly.interiors
                ],
            )
        )
    return scaled


def _clean_ring(coords) -> list[tuple[float, float]]:
    pts = [(float(x), float(y)) for x, y in coords]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    cleaned: list[tuple[float, float]] = []
    for p in pts:
        if not cleaned or (
            abs(cleaned[-1][0] - p[0]) > 1e-6 or abs(cleaned[-1][1] - p[1]) > 1e-6
        ):
            cleaned.append(p)
    if len(cleaned) >= 4 and cleaned[0] == cleaned[-1]:
        cleaned = cleaned[:-1]
    return cleaned


def _subdivide_ring(
    pts: list[tuple[float, float]], max_edge_mm: float
) -> list[tuple[float, float]]:
    """Insert midpoints until every edge is <= max_edge_mm."""
    if max_edge_mm <= 0 or len(pts) < 3:
        return pts
    out: list[tuple[float, float]] = []
    n = len(pts)
    for i in range(n):
        a = pts[i]
        b = pts[(i + 1) % n]
        out.append(a)
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        dist = math.hypot(dx, dy)
        if dist <= max_edge_mm:
            continue
        steps = max(2, int(math.ceil(dist / max_edge_mm)))
        for k in range(1, steps):
            t = k / steps
            out.append((a[0] + t * dx, a[1] + t * dy))
    return out


def _chaikin_closed(
    pts: list[tuple[float, float]], passes: int
) -> list[tuple[float, float]]:
    """Corner-cutting smoother for closed rings (rounds faceted arcs)."""
    if passes <= 0 or len(pts) < 3:
        return pts
    ring = list(pts)
    for _ in range(passes):
        nxt: list[tuple[float, float]] = []
        n = len(ring)
        for i in range(n):
            p0 = ring[i]
            p1 = ring[(i + 1) % n]
            nxt.append((0.75 * p0[0] + 0.25 * p1[0], 0.75 * p0[1] + 0.25 * p1[1]))
            nxt.append((0.25 * p0[0] + 0.75 * p1[0], 0.25 * p0[1] + 0.75 * p1[1]))
        ring = nxt
    return ring


def _ring_perimeter(pts: list[tuple[float, float]]) -> float:
    total = 0.0
    n = len(pts)
    for i in range(n):
        a = pts[i]
        b = pts[(i + 1) % n]
        total += math.hypot(b[0] - a[0], b[1] - a[1])
    return total


def _resample_ring(
    pts: list[tuple[float, float]],
    max_edge_mm: float,
    max_points: int,
) -> list[tuple[float, float]]:
    """Arc-length resample.

    Only *downsamples* dense contours. Linear upsampling of coarse polygons
    keeps flat chords (looks faceted) and STL export often merges collinear
    verts anyway — so never invent points between sparse vertices.
    """
    if len(pts) < 3:
        return pts

    n = len(pts)
    seg_lens = [
        math.hypot(pts[(i + 1) % n][0] - pts[i][0], pts[(i + 1) % n][1] - pts[i][1])
        for i in range(n)
    ]
    peri = sum(seg_lens)
    if peri <= 1e-9:
        return pts

    # Desired count from chord length; never above max_points.
    target = int(math.ceil(peri / max(max_edge_mm, 0.05)))
    target = max(24, target)
    if max_points > 0:
        target = min(target, max_points)

    # Already sparse or about right — keep source vertices (from the image).
    if n <= target:
        return pts

    cum = [0.0]
    for d in seg_lens:
        cum.append(cum[-1] + d)

    out: list[tuple[float, float]] = []
    for k in range(target):
        t = (k / target) * peri
        lo, hi = 0, n - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if cum[mid + 1] < t:
                lo = mid + 1
            else:
                hi = mid
        i = lo
        d = seg_lens[i]
        a = pts[i]
        b = pts[(i + 1) % n]
        u = 0.0 if d <= 1e-12 else (t - cum[i]) / d
        u = min(1.0, max(0.0, u))
        out.append((a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1])))

    return out if len(out) >= 3 else pts


def _decimate_ring(
    pts: list[tuple[float, float]],
    max_edge_mm: float,
    max_points: int,
) -> list[tuple[float, float]]:
    """Resample rings to a target chord length (smooth arcs, bounded size)."""
    return _resample_ring(pts, max_edge_mm, max_points)


def _smooth_polygon(
    poly: Polygon,
    max_edge_mm: float,
    curve_passes: int,
    max_ring_points: int = 480,
    letter_smooth_passes: int = 3,
    letter_max_edge_mm: float = 0.10,
    letter_max_points: int = 800,
    hole_max_points: int | None = None,
) -> Polygon:
    """Round letter curves; leave near-circular rings for analytic CAD circles."""
    if poly.is_empty or poly.geom_type != "Polygon":
        return poly

    if hole_max_points is None:
        hole_max_points = max(80, min(180, letter_max_points // 2))

    def _ring(coords, *, is_hole: bool = False) -> list[tuple[float, float]]:
        pts = _clean_ring(coords)
        if len(pts) < 3:
            return pts

        fitted = _fit_circle(pts)
        near_circle = fitted is not None and fitted[3] <= 0.025
        point_cap = hole_max_points if is_hole else letter_max_points
        edge_mm = letter_max_edge_mm if not near_circle else max_edge_mm
        ring_cap = max_ring_points if near_circle else point_cap

        if near_circle:
            # Seal rings → true CadQuery circles later; keep fit-stable outline only.
            return _decimate_ring(pts, max_edge_mm, max_ring_points)

        # Pre-cap BEFORE Chaikin. Chaikin doubles vertices each pass; applying it
        # to a 20k-point pixel contour creates huge rings and CadQuery freezes.
        pre_cap = min(700, max(point_cap * 2, 240))
        pts = _decimate_ring(pts, max(edge_mm, 0.15), pre_cap)

        passes = letter_smooth_passes if letter_smooth_passes > 0 else curve_passes
        if passes > 0:
            pts = _chaikin_closed(pts, min(passes, 3))
        pts = _decimate_ring(pts, edge_mm, ring_cap)
        if len(pts) < 3:
            return _clean_ring(coords)
        return pts

    try:
        outer = _ring(poly.exterior.coords, is_hole=False)
        holes = [_ring(h.coords, is_hole=True) for h in poly.interiors]
        holes = [h for h in holes if len(h) >= 3]
        smoothed = Polygon(outer, holes)
        if not smoothed.is_valid:
            smoothed = smoothed.buffer(0)
        if smoothed.is_empty:
            return poly
        if smoothed.geom_type == "Polygon":
            return smoothed
        if smoothed.geom_type == "MultiPolygon":
            parts = [g for g in smoothed.geoms if g.geom_type == "Polygon"]
            return max(parts, key=lambda g: g.area) if parts else poly
    except Exception:
        return poly
    return poly


def _smooth_polygons(
    polygons: list[Polygon],
    max_edge_mm: float,
    curve_passes: int,
    max_ring_points: int = 480,
    letter_smooth_passes: int = 3,
    letter_max_edge_mm: float = 0.10,
    letter_max_points: int = 800,
) -> list[Polygon]:
    out: list[Polygon] = []
    hole_cap = max(80, min(180, letter_max_points // 2))
    for poly in polygons:
        smoothed = _smooth_polygon(
            poly,
            max_edge_mm,
            curve_passes,
            max_ring_points,
            letter_smooth_passes=letter_smooth_passes,
            letter_max_edge_mm=letter_max_edge_mm,
            letter_max_points=letter_max_points,
            hole_max_points=hole_cap,
        )
        if smoothed.is_empty:
            continue
        if smoothed.geom_type == "Polygon":
            out.append(smoothed)
        elif smoothed.geom_type == "MultiPolygon":
            out.extend(g for g in smoothed.geoms if g.geom_type == "Polygon")
    return out if out else polygons


def _budget_polygon(
    poly: Polygon,
    *,
    max_total: int = 2200,
    max_exterior: int = 400,
    max_hole: int = 160,
    min_hole_area_mm2: float = 0.20,
) -> Polygon:
    """Drop tiny holes and cap vertex counts so OCCT extrusion stays interactive."""
    if poly.is_empty or poly.geom_type != "Polygon":
        return poly

    outer = _clean_ring(poly.exterior.coords)
    if len(outer) < 3:
        return poly

    holes: list[list[tuple[float, float]]] = []
    for interior in poly.interiors:
        hole_poly = Polygon(interior)
        if hole_poly.is_empty or hole_poly.area < min_hole_area_mm2:
            continue
        hole = _clean_ring(interior.coords)
        if len(hole) >= 3:
            holes.append(hole)

    # Prefer larger holes if we must drop some for the budget.
    holes.sort(key=lambda h: abs(Polygon(h).area), reverse=True)

    outer = _decimate_ring(outer, 0.12, max_exterior)
    capped_holes: list[list[tuple[float, float]]] = []
    used = len(outer)
    for hole in holes:
        room = max_total - used - 3
        if room < 12:
            break
        cap = min(max_hole, room)
        capped = _decimate_ring(hole, 0.15, cap)
        if len(capped) < 3:
            continue
        capped_holes.append(capped)
        used += len(capped)

    try:
        out = Polygon(outer, capped_holes)
        if not out.is_valid:
            out = out.buffer(0)
        if out.geom_type == "Polygon" and not out.is_empty:
            return out
        if out.geom_type == "MultiPolygon":
            parts = [g for g in out.geoms if g.geom_type == "Polygon"]
            return max(parts, key=lambda g: g.area) if parts else poly
    except Exception:
        return poly
    return poly


def _budget_polygons(
    polygons: list[Polygon],
    *,
    max_total: int = 2200,
    max_exterior: int = 400,
    max_hole: int = 160,
    min_hole_area_mm2: float = 0.20,
) -> list[Polygon]:
    out: list[Polygon] = []
    for poly in polygons:
        capped = _budget_polygon(
            poly,
            max_total=max_total,
            max_exterior=max_exterior,
            max_hole=max_hole,
            min_hole_area_mm2=min_hole_area_mm2,
        )
        if capped.is_empty:
            continue
        if capped.geom_type == "Polygon":
            out.append(capped)
        elif capped.geom_type == "MultiPolygon":
            out.extend(g for g in capped.geoms if g.geom_type == "Polygon")
    return out if out else polygons


def _tighten_close_concentric_rings(
    polygons: list[Polygon],
    max_gap_mm: float = 1.35,
    target_gap_mm: float = 0.55,
) -> list[Polygon]:
    """Pull apart-looking double seal rings back to a tight engraved gap.

    When blur/upscale widens the white band between two thin concentric rings,
    shrink that gap by expanding the inner ring outward and the outer ring inward.
    """
    from shapely.geometry import Point

    annuli: list[tuple[int, float, float, float, float, Polygon]] = []
    for i, poly in enumerate(polygons):
        if poly.is_empty or not poly.interiors:
            continue
        outer = _clean_ring(poly.exterior.coords)
        fitted = _fit_circle(outer)
        if fitted is None or fitted[3] > 0.03:
            continue
        cx, cy, r_out, _ = fitted
        hole = _clean_ring(list(poly.interiors)[0].coords)
        hf = _fit_circle(hole)
        if hf is None or hf[3] > 0.03:
            continue
        if math.hypot(hf[0] - cx, hf[1] - cy) > max(0.5, 0.03 * r_out):
            continue
        r_in = hf[2]
        if r_in >= r_out - 0.12:
            continue
        annuli.append((i, cx, cy, r_out, r_in, poly))

    if len(annuli) < 2:
        return polygons

    # Pair rings that are concentric and close (white gap between them).
    annuli.sort(key=lambda t: t[3], reverse=True)  # by outer radius
    replace: dict[int, Polygon] = {}
    used: set[int] = set()
    for a in range(len(annuli)):
        if a in used:
            continue
        i1, cx1, cy1, R1, r1, _p1 = annuli[a]
        for b in range(a + 1, len(annuli)):
            if b in used:
                continue
            i2, cx2, cy2, R2, r2, _p2 = annuli[b]
            if math.hypot(cx1 - cx2, cy1 - cy2) > 0.6:
                continue
            # Expect R1 >= r1 > R2 >= r2 with gap = r1 - R2
            if r1 <= R2:
                continue
            gap = r1 - R2
            if gap <= target_gap_mm or gap > max_gap_mm:
                continue
            # Split excess gap evenly: grow inner annulus outward, shrink outer inward.
            shrink = 0.5 * (gap - target_gap_mm)
            new_r1 = r1 - shrink
            new_R2 = R2 + shrink
            if new_r1 <= 0.2 or new_R2 >= R1 - 0.2 or new_R2 <= r2 + 0.2:
                continue
            res = 96  # shapely quarter-circle resolution → 384 segments
            outer_poly = Point(cx1, cy1).buffer(R1, resolution=res).difference(
                Point(cx1, cy1).buffer(new_r1, resolution=res)
            )
            inner_poly = Point(cx2, cy2).buffer(new_R2, resolution=res).difference(
                Point(cx2, cy2).buffer(r2, resolution=res)
            )
            if outer_poly.is_empty or inner_poly.is_empty:
                continue
            if outer_poly.geom_type == "Polygon":
                replace[i1] = outer_poly
            if inner_poly.geom_type == "Polygon":
                replace[i2] = inner_poly
            used.add(a)
            used.add(b)
            break

    if not replace:
        return polygons
    return [replace.get(i, p) for i, p in enumerate(polygons)]


def _merge_polygons(polygons: list[Polygon]) -> list[Polygon]:
    """Unary-union in Shapely so CadQuery does far fewer solid boolean ops."""
    if len(polygons) <= 1:
        return polygons
    try:
        merged = unary_union(polygons)
    except Exception:
        return polygons
    if merged is None or merged.is_empty:
        return polygons
    if merged.geom_type == "Polygon":
        return [merged]
    if merged.geom_type == "MultiPolygon":
        return [g for g in merged.geoms if g.geom_type == "Polygon" and not g.is_empty]
    if merged.geom_type == "GeometryCollection":
        out: list[Polygon] = []
        for g in merged.geoms:
            if g.geom_type == "Polygon" and not g.is_empty:
                out.append(g)
            elif g.geom_type == "MultiPolygon":
                out.extend(p for p in g.geoms if p.geom_type == "Polygon" and not p.is_empty)
        return out or polygons
    return polygons


def _fit_circle(
    pts: list[tuple[float, float]],
) -> tuple[float, float, float, float] | None:
    """Least-squares circle fit. Returns (cx, cy, r, max_rel_error) or None."""
    if len(pts) < 8:
        return None
    x = np.array([p[0] for p in pts], dtype=np.float64)
    y = np.array([p[1] for p in pts], dtype=np.float64)
    # Algebraic fit: x^2 + y^2 + D x + E y + F = 0
    A = np.column_stack([x, y, np.ones_like(x)])
    b = -(x * x + y * y)
    try:
        d, e, f = np.linalg.lstsq(A, b, rcond=None)[0]
    except Exception:
        return None
    cx = -0.5 * d
    cy = -0.5 * e
    rad2 = (d * d + e * e) / 4.0 - f
    if rad2 <= 1e-8:
        return None
    r = float(math.sqrt(rad2))
    if r < 0.3:
        return None
    err = np.abs(np.hypot(x - cx, y - cy) - r)
    return float(cx), float(cy), r, float(err.max() / r)


def _extrude_circular_polygon(
    poly: Polygon, depth: float, max_rel_err: float = 0.025
) -> cq.Workplane | None:
    """If exterior (+ holes) are near-circles, extrude true CadQuery circles."""
    if poly.is_empty or poly.geom_type != "Polygon":
        return None
    outer = _clean_ring(poly.exterior.coords)
    fitted = _fit_circle(outer)
    if fitted is None:
        return None
    cx, cy, r_out, err = fitted
    if err > max_rel_err:
        return None

    holes: list[float] = []
    for interior in poly.interiors:
        hole_pts = _clean_ring(interior.coords)
        hf = _fit_circle(hole_pts)
        if hf is None or hf[3] > max_rel_err:
            return None
        hx, hy, hr, _ = hf
        # Hole must share center with outer (concentric seal rings).
        if math.hypot(hx - cx, hy - cy) > max(0.5, 0.03 * r_out):
            return None
        # Allow thin engraved rings (seal borders can be < 1 mm wide).
        if hr >= r_out - 0.12:
            return None
        holes.append(hr)

    try:
        wp = cq.Workplane("XY").center(cx, cy).circle(r_out)
        for hr in sorted(holes, reverse=True):
            wp = wp.circle(hr)
        return wp.extrude(depth)
    except Exception:
        return None


def _extrude_polygon(poly: Polygon, depth: float) -> cq.Workplane | None:
    if poly.is_empty or poly.area <= 0:
        return None

    circular = _extrude_circular_polygon(poly, depth)
    if circular is not None:
        return circular

    outer = _clean_ring(poly.exterior.coords)
    if len(outer) < 3:
        return None

    holes = [
        _clean_ring(interior.coords)
        for interior in poly.interiors
        if len(_clean_ring(interior.coords)) >= 3
    ]

    # One extrusion with all wires is far faster than N sequential boolean cuts.
    try:
        wp = cq.Workplane("XY").polyline(outer).close()
        for hole in holes:
            wp = wp.polyline(hole).close()
        return wp.extrude(depth)
    except Exception:
        pass

    # Fallback: solid outer, then cut holes (slower, more tolerant of bad wires).
    try:
        wp = cq.Workplane("XY").polyline(outer).close().extrude(depth)
    except Exception:
        return None

    for hole in holes:
        try:
            cutter = cq.Workplane("XY").polyline(hole).close().extrude(depth + 0.01)
            wp = wp.cut(cutter)
        except Exception:
            continue

    return wp


def _raised_border_frame(
    width_mm: float,
    height_mm: float,
    border_width: float,
    depth: float,
) -> cq.Workplane:
    """Rectangular raised frame just outside the image canvas."""
    outer_w = width_mm + 2 * border_width
    outer_h = height_mm + 2 * border_width
    outer = cq.Workplane("XY").rect(outer_w, outer_h).extrude(depth)
    # Slightly taller cut so the inner opening is clean.
    inner = cq.Workplane("XY").rect(width_mm, height_mm).extrude(depth + 0.01)
    return outer.cut(inner)


def _union_relief(parts: list[cq.Workplane]) -> cq.Workplane:
    if len(parts) == 1:
        return parts[0]
    # Fuse solids in one shot when possible — much faster than N sequential unions.
    try:
        solids = [p.val() for p in parts if p is not None and p.val() is not None]
        if not solids:
            return parts[0]
        if len(solids) == 1:
            return cq.Workplane(obj=solids[0])
        fused = solids[0].fuse(*solids[1:])
        return cq.Workplane(obj=fused)
    except Exception:
        relief = parts[0]
        for part in parts[1:]:
            try:
                relief = relief.union(part)
            except Exception:
                continue
        return relief


def _finish_stamp(
    relief: cq.Workplane,
    settings: ImageStampSettings,
    canvas_width_mm: float,
    canvas_height_mm: float,
) -> cq.Workplane:
    if settings.raised_border:
        frame = _raised_border_frame(
            canvas_width_mm,
            canvas_height_mm,
            settings.border_width,
            settings.imprint_depth,
        )
        relief = relief.union(frame)

    relief = apply_mirror(relief, settings.mirror_for_leather)

    solid = relief.val()
    if solid is None:
        raise StampGenerationError("Image relief produced no geometry.")

    bb = solid.BoundingBox()
    if bb.xlen <= 0 or bb.ylen <= 0:
        raise StampGenerationError("Image bounds are invalid.")

    base = build_base_plate(bb, settings.margin, settings.base_thickness)
    return base.union(relief)


def _build_from_svg(settings: ImageStampSettings) -> cq.Workplane:
    polygons, src_w, src_h = svg_to_polygons(settings.image_path)
    if settings.invert:
        polygons = invert_polygons(polygons, src_w, src_h)
        if not polygons:
            raise StampGenerationError("Invert left no shapes to extrude.")

    polygons = flip_y_polygons(polygons, src_h)
    polygons, height_mm = scale_polygons_to_width(
        polygons, src_w, src_h, settings.width_mm
    )
    polygons = _smooth_polygons(
        polygons,
        settings.max_edge_mm,
        settings.curve_smooth_passes,
        settings.max_ring_points,
        letter_smooth_passes=settings.letter_smooth_passes,
        letter_max_edge_mm=settings.letter_max_edge_mm,
        letter_max_points=settings.letter_max_points,
    )
    polygons = _merge_polygons(polygons)
    polygons = _budget_polygons(
        polygons,
        max_total=settings.max_total_vertices,
        max_exterior=settings.letter_max_points,
        max_hole=max(80, settings.letter_max_points // 2),
        min_hole_area_mm2=settings.min_hole_area_mm2,
    )

    relief_parts: list[cq.Workplane] = []
    for poly in polygons:
        part = _extrude_polygon(poly, settings.imprint_depth)
        if part is not None:
            relief_parts.append(part)

    if not relief_parts:
        raise StampGenerationError("Could not extrude SVG shapes.")

    return _finish_stamp(
        _union_relief(relief_parts), settings, settings.width_mm, height_mm
    )


def _build_from_raster(settings: ImageStampSettings) -> cq.Workplane:
    gray = _load_grayscale_array(settings.image_path, settings.max_pixels)
    mask = _binary_mask(gray, settings.threshold, settings.invert)
    mask = _smooth_mask_edges(mask, settings.edge_smooth_px)
    polygons = _contours_to_polygons(mask, settings.simplify)
    if not polygons:
        raise StampGenerationError(
            "No stamp shapes found. Adjust threshold/invert or use a higher-contrast image."
        )

    # IMPORTANT: scale using mask.shape — edge smoothing may supersample the mask.
    img_h, img_w = mask.shape
    height_mm = settings.width_mm * (img_h / img_w) if img_w > 0 else settings.width_mm
    polygons = _scale_polygons(polygons, settings.width_mm, mask.shape)
    polygons = _smooth_polygons(
        polygons,
        settings.max_edge_mm,
        settings.curve_smooth_passes,
        settings.max_ring_points,
        letter_smooth_passes=settings.letter_smooth_passes,
        letter_max_edge_mm=settings.letter_max_edge_mm,
        letter_max_points=settings.letter_max_points,
    )
    polygons = _tighten_close_concentric_rings(polygons)
    polygons = _merge_polygons(polygons)
    polygons = _budget_polygons(
        polygons,
        max_total=settings.max_total_vertices,
        max_exterior=settings.letter_max_points,
        max_hole=max(80, settings.letter_max_points // 2),
        min_hole_area_mm2=settings.min_hole_area_mm2,
    )
    relief_parts: list[cq.Workplane] = []
    for poly in polygons:
        part = _extrude_polygon(poly, settings.imprint_depth)
        if part is not None:
            relief_parts.append(part)

    if not relief_parts:
        raise StampGenerationError("Could not extrude image shapes.")

    return _finish_stamp(
        _union_relief(relief_parts), settings, settings.width_mm, height_mm
    )


def build_image_stamp(settings: ImageStampSettings) -> cq.Workplane:
    """Build a stamp from a bitmap silhouette or SVG vector paths."""
    _validate_settings(settings)
    if is_svg_path(settings.image_path):
        return _build_from_svg(settings)
    return _build_from_raster(settings)


def preview_image_mask(settings: ImageStampSettings) -> np.ndarray:
    """Return binary mask for 2D preview in the UI (includes optional border)."""
    if is_svg_path(settings.image_path):
        polygons, src_w, src_h = svg_to_polygons(settings.image_path)
        if settings.invert:
            polygons = invert_polygons(polygons, src_w, src_h)
        mask = rasterize_polygons(polygons, src_w, src_h, settings.max_pixels)
    else:
        gray = _load_grayscale_array(settings.image_path, settings.max_pixels)
        mask = _binary_mask(gray, settings.threshold, settings.invert)
        mask = _smooth_mask_edges(mask, settings.edge_smooth_px)

    if not settings.raised_border or settings.border_width <= 0:
        return mask

    img_h, img_w = mask.shape
    if img_w <= 0:
        return mask
    px_per_mm = img_w / settings.width_mm
    border_px = max(1, int(round(settings.border_width * px_per_mm)))
    framed = np.zeros((img_h + 2 * border_px, img_w + 2 * border_px), dtype=np.uint8)
    framed[border_px : border_px + img_h, border_px : border_px + img_w] = mask
    framed[:border_px, :] = 255
    framed[-border_px:, :] = 255
    framed[:, :border_px] = 255
    framed[:, -border_px:] = 255
    return framed
