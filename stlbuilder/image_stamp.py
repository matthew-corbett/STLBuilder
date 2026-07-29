"""Convert imported images into raised-relief stamp geometry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cadquery as cq
import cv2
import numpy as np
from PIL import Image
from shapely.geometry import Polygon

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
    simplify: float = 0.15
    max_pixels: int = 1000
    raised_border: bool = False
    border_width: float = 1.5


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
    if longest > max_pixels:
        scale = max_pixels / longest
        new_w = max(1, int(width * scale))
        new_h = max(1, int(height * scale))
        gray = cv2.resize(gray, (new_w, new_h), interpolation=cv2.INTER_AREA)

    return gray


def _binary_mask(gray: np.ndarray, threshold: int, invert: bool) -> np.ndarray:
    if invert:
        mask = (gray >= threshold).astype(np.uint8) * 255
    else:
        mask = (gray < threshold).astype(np.uint8) * 255
    return mask


def _simplify_contour(cnt: np.ndarray, simplify: float) -> np.ndarray:
    """Reduce contour vertices. simplify is percent of perimeter (0 = none)."""
    if simplify <= 0 or len(cnt) < 3:
        return cnt

    perimeter = cv2.arcLength(cnt, True)
    if perimeter <= 0:
        return cnt

    epsilon = (simplify / 100.0) * perimeter
    # Small features (stars, serifs) keep sharper corners than large regions.
    if perimeter < 120:
        epsilon = min(epsilon, perimeter * 0.025)

    approx = cv2.approxPolyDP(cnt, epsilon, True)
    return approx if len(approx) >= 3 else cnt


def _contours_to_polygons(
    mask: np.ndarray,
    simplify: float,
) -> list[Polygon]:
    """Extract filled regions (with holes) as shapely polygons in pixel coords."""
    contours, hierarchy = cv2.findContours(
        mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
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
        if poly.is_empty or poly.area < 4:
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)
            if poly.is_empty or poly.geom_type != "Polygon":
                continue
        polygons.append(poly)

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


def _extrude_polygon(poly: Polygon, depth: float) -> cq.Workplane | None:
    if poly.is_empty or poly.area <= 0:
        return None

    outer = _clean_ring(poly.exterior.coords)
    if len(outer) < 3:
        return None

    try:
        wp = cq.Workplane("XY").polyline(outer).close().extrude(depth)
    except Exception:
        return None

    for interior in poly.interiors:
        hole = _clean_ring(interior.coords)
        if len(hole) < 3:
            continue
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
    relief = parts[0]
    for part in parts[1:]:
        relief = relief.union(part)
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
    polygons = _contours_to_polygons(mask, settings.simplify)
    if not polygons:
        raise StampGenerationError(
            "No stamp shapes found. Adjust threshold/invert or use a higher-contrast image."
        )

    img_h, img_w = gray.shape
    height_mm = settings.width_mm * (img_h / img_w) if img_w > 0 else settings.width_mm
    polygons = _scale_polygons(polygons, settings.width_mm, gray.shape)
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
