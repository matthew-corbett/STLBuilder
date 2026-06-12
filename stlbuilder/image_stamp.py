"""Convert imported images into raised-relief stamp geometry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cadquery as cq
import cv2
import numpy as np
from PIL import Image
from shapely.geometry import Polygon
from shapely.validation import make_valid

from stlbuilder.geometry_utils import apply_mirror, build_base_plate
from stlbuilder.stamp_generator import StampGenerationError


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
    simplify: float = 0.8
    max_pixels: int = 400


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
        epsilon = max(simplify, 0.1)
        approx = cv2.approxPolyDP(cnt, epsilon, True)
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
            poly = make_valid(poly)
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


def _extrude_polygon(poly: Polygon, depth: float) -> cq.Workplane | None:
    if poly.is_empty or poly.area <= 0:
        return None

    outer = [(float(x), float(y)) for x, y in poly.exterior.coords]
    if len(outer) < 3:
        return None

    wp = cq.Workplane("XY").polyline(outer).close().extrude(depth)

    for interior in poly.interiors:
        hole = [(float(x), float(y)) for x, y in interior.coords]
        if len(hole) >= 3:
            wp = wp.cut(cq.Workplane("XY").polyline(hole).close().extrude(depth + 0.01))

    return wp


def build_image_stamp(settings: ImageStampSettings) -> cq.Workplane:
    """Build a stamp from a bitmap silhouette (dark areas become raised relief)."""
    _validate_settings(settings)

    gray = _load_grayscale_array(settings.image_path, settings.max_pixels)
    mask = _binary_mask(gray, settings.threshold, settings.invert)
    polygons = _contours_to_polygons(mask, settings.simplify)
    if not polygons:
        raise StampGenerationError(
            "No stamp shapes found. Adjust threshold/invert or use a higher-contrast image."
        )

    polygons = _scale_polygons(polygons, settings.width_mm, gray.shape)
    relief_parts: list[cq.Workplane] = []
    for poly in polygons:
        part = _extrude_polygon(poly, settings.imprint_depth)
        if part is not None:
            relief_parts.append(part)

    if not relief_parts:
        raise StampGenerationError("Could not extrude image shapes.")

    relief = relief_parts[0]
    for part in relief_parts[1:]:
        relief = relief.union(part)

    relief = apply_mirror(relief, settings.mirror_for_leather)

    solid = relief.val()
    if solid is None:
        raise StampGenerationError("Image relief produced no geometry.")

    bb = solid.BoundingBox()
    if bb.xlen <= 0 or bb.ylen <= 0:
        raise StampGenerationError("Image bounds are invalid.")

    base = build_base_plate(bb, settings.margin, settings.base_thickness)
    return base.union(relief)


def preview_image_mask(settings: ImageStampSettings) -> np.ndarray:
    """Return binary mask for 2D preview in the UI."""
    gray = _load_grayscale_array(settings.image_path, settings.max_pixels)
    return _binary_mask(gray, settings.threshold, settings.invert)
