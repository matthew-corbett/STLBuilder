"""CustomTkinter front-end for leather stamps and valet-tray molds."""

from __future__ import annotations

import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import numpy as np
import trimesh
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from PIL import Image, ImageOps

from stlbuilder.draft import collect_settings_dict, load_draft, save_draft
from stlbuilder.fonts import FontOption, list_system_fonts
from stlbuilder.image_stamp import ImageStampSettings, build_image_stamp, preview_image_mask
from stlbuilder.svg_stamp import is_svg_path
from stlbuilder.stamp_generator import (
    StampGenerationError,
    StampSettings,
    build_stamp,
    export_stl,
    model_to_trimesh,
)
from stlbuilder.valet_mold import (
    SHAPE_OVAL,
    SHAPE_RECT,
    SHAPE_ROUNDED_RECT,
    SHAPE_SILHOUETTE,
    ValetMoldSettings,
    build_valet_mold_set,
)


class StampDesignerApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.title("STL Builder — Stamps & Valet Molds")
        self.geometry("1150x760")
        self.minsize(950, 640)

        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self._fonts: list[FontOption] = list_system_fonts()
        self._custom_font_path: str | None = None
        self._image_path: str | None = None
        self._image_preview_ref: ctk.CTkImage | None = None
        self._mold_preview_ref: ctk.CTkImage | None = None
        self._mode = ctk.StringVar(value="text")
        self._model = None
        self._female_model = None
        self._busy = False
        self._draft_workspace = None

        self._build_layout()
        self._on_mode_changed("text")
        self._set_status("Ready. Design a stamp or mold and click Update Preview.")

    def _build_layout(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkScrollableFrame(self, width=340, label_text="Design settings")
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=12)
        sidebar.grid_columnconfigure(0, weight=1)

        preview_frame = ctk.CTkFrame(self)
        preview_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)
        preview_frame.grid_rowconfigure(1, weight=1)
        preview_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            preview_frame,
            text="3D Preview",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))

        self._fig = Figure(figsize=(6, 5), dpi=100)
        self._ax = self._fig.add_subplot(111, projection="3d")
        self._canvas = FigureCanvasTkAgg(self._fig, master=preview_frame)
        self._canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew", padx=8, pady=8)

        btn_row = ctk.CTkFrame(preview_frame, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        btn_row.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self._preview_btn = ctk.CTkButton(
            btn_row, text="Update Preview", command=self._on_preview
        )
        self._preview_btn.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        self._export_btn = ctk.CTkButton(
            btn_row, text="Export STL…", command=self._on_export, fg_color="#2d6a4f"
        )
        self._export_btn.grid(row=0, column=1, padx=4, sticky="ew")

        self._save_draft_btn = ctk.CTkButton(
            btn_row, text="Save Draft…", command=self._on_save_draft
        )
        self._save_draft_btn.grid(row=0, column=2, padx=4, sticky="ew")

        self._open_draft_btn = ctk.CTkButton(
            btn_row, text="Open Draft…", command=self._on_open_draft
        )
        self._open_draft_btn.grid(row=0, column=3, padx=(4, 0), sticky="ew")

        self._status = ctk.CTkLabel(self, text="", anchor="w")
        self._status.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 10))

        row = 0

        ctk.CTkLabel(sidebar, text="Mode").grid(row=row, column=0, sticky="w", pady=(4, 0))
        row += 1
        ctk.CTkSegmentedButton(
            sidebar,
            values=["text", "image", "mold"],
            variable=self._mode,
            command=self._on_mode_changed,
        ).grid(row=row, column=0, sticky="ew", pady=4)
        row += 1

        self._text_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        self._text_frame.grid(row=row, column=0, sticky="ew")
        self._text_frame.grid_columnconfigure(0, weight=1)
        text_row = 0

        ctk.CTkLabel(self._text_frame, text="Stamp text").grid(
            row=text_row, column=0, sticky="w", pady=(4, 0)
        )
        text_row += 1
        self._text = ctk.CTkTextbox(self._text_frame, height=100)
        self._text.grid(row=text_row, column=0, sticky="ew", pady=4)
        self._text.insert("1.0", "Your Name")
        text_row += 1

        ctk.CTkLabel(
            self._text_frame,
            text="Use Enter for a second line",
            font=ctk.CTkFont(size=11),
            text_color="gray",
        ).grid(row=text_row, column=0, sticky="w")
        text_row += 1

        ctk.CTkLabel(self._text_frame, text="Font").grid(
            row=text_row, column=0, sticky="w", pady=(8, 0)
        )
        text_row += 1
        font_names = [f.display_name for f in self._fonts]
        default_idx = font_names.index("Arial") if "Arial" in font_names else 0
        self._font_var = ctk.StringVar(value=font_names[default_idx])
        self._font_menu = ctk.CTkOptionMenu(
            self._text_frame,
            variable=self._font_var,
            values=font_names,
            width=280,
            command=self._on_font_selected,
        )
        self._font_menu.grid(row=text_row, column=0, sticky="ew", pady=4)
        text_row += 1

        ctk.CTkButton(
            self._text_frame, text="Load custom .ttf…", command=self._on_load_font, height=28
        ).grid(row=text_row, column=0, sticky="ew", pady=2)
        text_row += 1

        self._custom_font_label = ctk.CTkLabel(
            self._text_frame,
            text="No custom font loaded",
            font=ctk.CTkFont(size=11),
            text_color="gray",
        )
        self._custom_font_label.grid(row=text_row, column=0, sticky="w")
        text_row += 1

        ctk.CTkLabel(self._text_frame, text="Text direction").grid(
            row=text_row, column=0, sticky="w", pady=(8, 0)
        )
        text_row += 1
        self._orientation = ctk.StringVar(value="horizontal")
        ctk.CTkSegmentedButton(
            self._text_frame,
            values=["horizontal", "vertical"],
            variable=self._orientation,
        ).grid(row=text_row, column=0, sticky="ew", pady=4)
        text_row += 1

        ctk.CTkLabel(
            self._text_frame,
            text="Vertical stacks each letter top-to-bottom.",
            font=ctk.CTkFont(size=11),
            text_color="gray",
        ).grid(row=text_row, column=0, sticky="w")
        text_row += 1

        self._font_size = self._labeled_slider(
            self._text_frame, text_row, "Font size (mm)", 6, 40, 12
        )

        self._image_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        self._image_frame.grid(row=row, column=0, sticky="ew")
        self._image_frame.grid_columnconfigure(0, weight=1)
        image_row = 0

        ctk.CTkButton(
            self._image_frame,
            text="Import image / SVG…",
            command=self._on_load_image,
            height=32,
        ).grid(row=image_row, column=0, sticky="ew", pady=(4, 2))
        image_row += 1

        self._image_name_label = ctk.CTkLabel(
            self._image_frame,
            text="No image loaded",
            font=ctk.CTkFont(size=11),
            text_color="gray",
            wraplength=300,
            justify="left",
        )
        self._image_name_label.grid(row=image_row, column=0, sticky="w")
        image_row += 1

        self._image_preview_label = ctk.CTkLabel(
            self._image_frame,
            text="Mask preview",
            width=280,
            height=140,
            fg_color=("gray90", "gray20"),
            corner_radius=6,
        )
        self._image_preview_label.grid(row=image_row, column=0, sticky="ew", pady=6)
        image_row += 1

        self._stamp_width = self._labeled_slider(
            self._image_frame, image_row, "Stamp width (mm)", 10, 120, 40
        )
        image_row += 1

        self._raster_controls = ctk.CTkFrame(self._image_frame, fg_color="transparent")
        self._raster_controls.grid(row=image_row, column=0, sticky="ew")
        self._raster_controls.grid_columnconfigure(0, weight=1)
        raster_row = 0

        self._threshold = self._labeled_slider(
            self._raster_controls, raster_row, "Threshold", 0, 255, 128
        )
        raster_row += 1

        self._simplify = self._labeled_slider(
            self._raster_controls,
            raster_row,
            "Edge simplify (%)",
            0.0,
            2.0,
            0.0,
            resolution=0.05,
        )
        raster_row += 1
        image_row += 1

        self._invert_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self._image_frame,
            text="Invert (raise light / background instead)",
            variable=self._invert_var,
            command=self._refresh_image_preview,
        ).grid(row=image_row, column=0, sticky="w", pady=4)
        image_row += 1

        self._raised_border_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self._image_frame,
            text="Raised border around image",
            variable=self._raised_border_var,
            command=self._refresh_image_preview,
        ).grid(row=image_row, column=0, sticky="w", pady=4)
        image_row += 1

        self._border_width = self._labeled_slider(
            self._image_frame, image_row, "Border width (mm)", 0.5, 5.0, 1.5, resolution=0.1
        )
        image_row += 1

        self._image_hint = ctk.CTkLabel(
            self._image_frame,
            text=(
                "PNG/JPG: small logos are upscaled before contouring so letter curves "
                "stay smooth (seal rings use true circles). Prefer 1500px+ sources."
            ),
            wraplength=300,
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color="gray",
        )
        self._image_hint.grid(row=image_row, column=0, sticky="w", pady=2)

        self._mold_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        self._mold_frame.grid(row=row, column=0, sticky="ew")
        self._mold_frame.grid_columnconfigure(0, weight=1)
        mold_row = 0

        ctk.CTkLabel(self._mold_frame, text="Mold outline").grid(
            row=mold_row, column=0, sticky="w", pady=(4, 0)
        )
        mold_row += 1
        self._mold_shape = ctk.StringVar(value=SHAPE_ROUNDED_RECT)
        self._mold_shape_menu = ctk.CTkOptionMenu(
            self._mold_frame,
            variable=self._mold_shape,
            values=[SHAPE_RECT, SHAPE_ROUNDED_RECT, SHAPE_OVAL, SHAPE_SILHOUETTE],
            command=self._on_mold_shape_changed,
            width=280,
        )
        self._mold_shape_menu.grid(row=mold_row, column=0, sticky="ew", pady=4)
        mold_row += 1

        self._mold_size_frame = ctk.CTkFrame(self._mold_frame, fg_color="transparent")
        self._mold_size_frame.grid(row=mold_row, column=0, sticky="ew")
        self._mold_size_frame.grid_columnconfigure(0, weight=1)
        size_row = 0
        self._mold_length = self._labeled_slider(
            self._mold_size_frame, size_row, "Length (mm)", 40, 300, 180
        )
        size_row += 1
        self._mold_width = self._labeled_slider(
            self._mold_size_frame, size_row, "Width (mm)", 40, 250, 130
        )
        size_row += 1
        self._mold_corner = self._labeled_slider(
            self._mold_size_frame, size_row, "Corner radius (mm)", 0, 60, 12
        )
        mold_row += 1

        self._mold_silhouette_frame = ctk.CTkFrame(self._mold_frame, fg_color="transparent")
        self._mold_silhouette_frame.grid(row=mold_row, column=0, sticky="ew")
        self._mold_silhouette_frame.grid_columnconfigure(0, weight=1)
        sil_row = 0
        ctk.CTkButton(
            self._mold_silhouette_frame,
            text="Import silhouette / SVG…",
            command=self._on_load_mold_silhouette,
            height=32,
        ).grid(row=sil_row, column=0, sticky="ew", pady=(4, 2))
        sil_row += 1
        self._mold_image_name_label = ctk.CTkLabel(
            self._mold_silhouette_frame,
            text="No silhouette loaded",
            font=ctk.CTkFont(size=11),
            text_color="gray",
            wraplength=300,
            justify="left",
        )
        self._mold_image_name_label.grid(row=sil_row, column=0, sticky="w")
        sil_row += 1
        self._mold_image_preview_label = ctk.CTkLabel(
            self._mold_silhouette_frame,
            text="Outline preview",
            width=280,
            height=120,
            fg_color=("gray90", "gray20"),
            corner_radius=6,
        )
        self._mold_image_preview_label.grid(row=sil_row, column=0, sticky="ew", pady=6)
        sil_row += 1
        self._mold_target_width = self._labeled_slider(
            self._mold_silhouette_frame, sil_row, "Silhouette width (mm)", 40, 300, 180
        )
        sil_row += 1
        self._mold_raster_controls = ctk.CTkFrame(
            self._mold_silhouette_frame, fg_color="transparent"
        )
        self._mold_raster_controls.grid(row=sil_row, column=0, sticky="ew")
        self._mold_raster_controls.grid_columnconfigure(0, weight=1)
        mold_raster_row = 0
        self._mold_threshold = self._labeled_slider(
            self._mold_raster_controls, mold_raster_row, "Threshold", 0, 255, 128
        )
        mold_raster_row += 1
        self._mold_simplify = self._labeled_slider(
            self._mold_raster_controls,
            mold_raster_row,
            "Edge simplify (%)",
            0.0,
            2.0,
            0.0,
            resolution=0.05,
        )
        mold_raster_row += 1
        self._mold_invert_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self._mold_raster_controls,
            text="Invert silhouette",
            variable=self._mold_invert_var,
            command=self._refresh_mold_preview,
        ).grid(row=mold_raster_row, column=0, sticky="w", pady=4)
        mold_row += 1

        self._mold_height = self._labeled_slider(
            self._mold_frame, mold_row, "Mold height (mm)", 8, 40, 18
        )
        mold_row += 1
        self._mold_draft = self._labeled_slider(
            self._mold_frame, mold_row, "Draft angle (°)", 0, 10, 2, resolution=0.5
        )
        mold_row += 1
        self._mold_fillet = self._labeled_slider(
            self._mold_frame, mold_row, "Top edge fillet (mm)", 0, 6, 2, resolution=0.5
        )
        mold_row += 1
        self._mold_flange = self._labeled_slider(
            self._mold_frame, mold_row, "Base flange (mm)", 0, 25, 8
        )
        mold_row += 1
        self._mold_leather = self._labeled_slider(
            self._mold_frame,
            mold_row,
            "Leather thickness (mm)",
            1.0,
            6.0,
            3.2,
            resolution=0.1,
        )
        mold_row += 1
        self._mold_female_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self._mold_frame,
            text="Also build female collar",
            variable=self._mold_female_var,
        ).grid(row=mold_row, column=0, sticky="w", pady=6)
        mold_row += 1
        self._mold_hint = ctk.CTkLabel(
            self._mold_frame,
            text=(
                "Wet-form leather over the male plug. Optional female collar "
                "clamps leather around it (~8oz ≈ 3.2 mm)."
            ),
            wraplength=300,
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color="gray",
        )
        self._mold_hint.grid(row=mold_row, column=0, sticky="w", pady=2)

        row += 1
        self._shared_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        self._shared_frame.grid(row=row, column=0, sticky="ew")
        self._shared_frame.grid_columnconfigure(0, weight=1)
        shared_row = 0

        self._imprint_depth = self._labeled_slider(
            self._shared_frame, shared_row, "Imprint depth (mm)", 0.5, 8, 2.0, resolution=0.1
        )
        shared_row += 1

        self._base_thickness = self._labeled_slider(
            self._shared_frame, shared_row, "Base thickness (mm)", 2, 15, 5
        )
        shared_row += 1

        self._margin = self._labeled_slider(
            self._shared_frame, shared_row, "Border margin (mm)", 0, 20, 4
        )
        shared_row += 1

        self._mirror_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            self._shared_frame,
            text="Mirror design (reads correctly on leather)",
            variable=self._mirror_var,
        ).grid(row=shared_row, column=0, sticky="w", pady=8)
        shared_row += 1

        self._shared_hint = ctk.CTkLabel(
            self._shared_frame,
            text=(
                "Raised areas extrude upward from the stamp face. "
                "Print with the design side up. Mirror flips the design so "
                "the leather impression reads normally."
            ),
            wraplength=300,
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color="gray",
        )
        self._shared_hint.grid(row=shared_row, column=0, sticky="w", pady=4)

        self._bind_slider_refresh(self._threshold, self._refresh_image_preview)
        self._bind_slider_refresh(self._simplify, self._refresh_image_preview)
        self._bind_slider_refresh(self._stamp_width, self._refresh_image_preview)
        self._bind_slider_refresh(self._border_width, self._refresh_image_preview)
        self._bind_slider_refresh(self._mold_threshold, self._refresh_mold_preview)
        self._bind_slider_refresh(self._mold_simplify, self._refresh_mold_preview)
        self._bind_slider_refresh(self._mold_target_width, self._refresh_mold_preview)

        self._on_mold_shape_changed(self._mold_shape.get())

    def _bind_slider_refresh(self, slider: ctk.CTkSlider, callback) -> None:
        original = slider.cget("command")

        def wrapped(value: float) -> None:
            if original:
                original(value)
            callback()

        slider.configure(command=wrapped)

    def _on_mode_changed(self, mode: str) -> None:
        self._text_frame.grid_remove()
        self._image_frame.grid_remove()
        self._mold_frame.grid_remove()

        if mode == "text":
            self._text_frame.grid()
            self._shared_frame.grid()
        elif mode == "image":
            self._image_frame.grid()
            self._shared_frame.grid()
            self._update_image_controls_for_source()
            self._refresh_image_preview()
        else:
            self._mold_frame.grid()
            self._shared_frame.grid_remove()
            self._on_mold_shape_changed(self._mold_shape.get())

    def _on_mold_shape_changed(self, shape: str) -> None:
        if shape == SHAPE_SILHOUETTE:
            self._mold_size_frame.grid_remove()
            self._mold_silhouette_frame.grid()
            self._update_mold_controls_for_source()
            self._refresh_mold_preview()
            return

        self._mold_silhouette_frame.grid_remove()
        self._mold_size_frame.grid()
        # Corner radius only applies to rounded rectangles.
        corner_parent = self._mold_corner.master
        if shape == SHAPE_ROUNDED_RECT:
            corner_parent.grid()
        else:
            corner_parent.grid_remove()

    def _labeled_slider(
        self,
        parent: ctk.CTkFrame,
        row: int,
        label: str,
        low: float,
        high: float,
        default: float,
        resolution: float = 1.0,
    ) -> ctk.CTkSlider:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        frame.grid_columnconfigure(1, weight=1)

        value_var = ctk.StringVar(value=f"{default:g}")

        def on_change(v: float) -> None:
            if resolution < 1:
                value_var.set(f"{float(v):.1f}")
            else:
                value_var.set(f"{int(round(v))}")

        ctk.CTkLabel(frame, text=label).grid(row=0, column=0, columnspan=2, sticky="w")
        ctk.CTkLabel(frame, textvariable=value_var, width=48).grid(
            row=0, column=2, sticky="e"
        )

        steps = int((high - low) / resolution) if resolution > 0 else 100
        slider = ctk.CTkSlider(
            frame,
            from_=low,
            to=high,
            number_of_steps=max(steps, 1),
            command=on_change,
        )
        slider.set(default)
        on_change(default)
        slider.grid(row=1, column=0, columnspan=3, sticky="ew", pady=4)
        slider.value_var = value_var  # type: ignore[attr-defined]
        slider._fmt_resolution = resolution  # type: ignore[attr-defined]
        return slider

    def _slider_value(self, slider: ctk.CTkSlider) -> float:
        return float(slider.get())

    def _set_slider(self, slider: ctk.CTkSlider, value: float) -> None:
        low = float(slider.cget("from_"))
        high = float(slider.cget("to"))
        clamped = max(low, min(high, float(value)))
        slider.set(clamped)
        resolution = getattr(slider, "_fmt_resolution", 1.0)
        value_var = getattr(slider, "value_var", None)
        if value_var is not None:
            if resolution < 1:
                value_var.set(f"{clamped:.1f}")
            else:
                value_var.set(f"{int(round(clamped))}")

    def _collect_text_settings(self) -> StampSettings:
        text = self._text.get("1.0", "end").strip()
        family = self._font_var.get()
        if self._custom_font_path:
            family = Path(self._custom_font_path).stem

        return StampSettings(
            text=text,
            font_family=family,
            font_path=self._custom_font_path,
            font_size=self._slider_value(self._font_size),
            imprint_depth=self._slider_value(self._imprint_depth),
            base_thickness=self._slider_value(self._base_thickness),
            margin=self._slider_value(self._margin),
            mirror_for_leather=self._mirror_var.get(),
            orientation=self._orientation.get(),
        )

    def _collect_image_settings(self) -> ImageStampSettings:
        if not self._image_path:
            raise StampGenerationError("Import an image to create an image stamp.")

        return ImageStampSettings(
            image_path=self._image_path,
            width_mm=self._slider_value(self._stamp_width),
            imprint_depth=self._slider_value(self._imprint_depth),
            base_thickness=self._slider_value(self._base_thickness),
            margin=self._slider_value(self._margin),
            mirror_for_leather=self._mirror_var.get(),
            threshold=int(round(self._slider_value(self._threshold))),
            invert=self._invert_var.get(),
            simplify=self._slider_value(self._simplify),
            raised_border=self._raised_border_var.get(),
            border_width=self._slider_value(self._border_width),
        )

    def _collect_mold_settings(self) -> ValetMoldSettings:
        shape = self._mold_shape.get()
        return ValetMoldSettings(
            shape=shape,
            length_mm=self._slider_value(self._mold_length),
            width_mm=self._slider_value(self._mold_width),
            height_mm=self._slider_value(self._mold_height),
            corner_radius_mm=self._slider_value(self._mold_corner),
            draft_deg=self._slider_value(self._mold_draft),
            top_fillet_mm=self._slider_value(self._mold_fillet),
            flange_mm=self._slider_value(self._mold_flange),
            leather_thickness_mm=self._slider_value(self._mold_leather),
            include_female=self._mold_female_var.get(),
            image_path=self._image_path,
            target_width_mm=self._slider_value(self._mold_target_width),
            threshold=int(round(self._slider_value(self._mold_threshold))),
            invert=self._mold_invert_var.get(),
            simplify=self._slider_value(self._mold_simplify),
        )

    def _on_font_selected(self, _choice: str) -> None:
        self._custom_font_path = None
        self._custom_font_label.configure(
            text="No custom font loaded", text_color="gray"
        )

    def _on_load_font(self) -> None:
        path = filedialog.askopenfilename(
            title="Select TrueType font",
            filetypes=[("TrueType fonts", "*.ttf"), ("All files", "*.*")],
        )
        if path:
            self._custom_font_path = path
            self._custom_font_label.configure(
                text=f"Custom: {Path(path).name}", text_color=("gray10", "gray90")
            )

    def _on_load_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Select stamp image or SVG",
            filetypes=[
                ("Images & SVG", "*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp;*.svg"),
                ("SVG vector", "*.svg"),
                ("Raster images", "*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._image_path = path
            self._image_name_label.configure(
                text=Path(path).name, text_color=("gray10", "gray90")
            )
            self._mode.set("image")
            self._on_mode_changed("image")
            self._update_image_controls_for_source()
            self._refresh_image_preview()

    def _on_load_mold_silhouette(self) -> None:
        path = filedialog.askopenfilename(
            title="Select mold silhouette or SVG",
            filetypes=[
                ("Images & SVG", "*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp;*.svg"),
                ("SVG vector", "*.svg"),
                ("Raster images", "*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._image_path = path
            self._mold_image_name_label.configure(
                text=Path(path).name, text_color=("gray10", "gray90")
            )
            self._mold_shape.set(SHAPE_SILHOUETTE)
            self._mode.set("mold")
            self._on_mode_changed("mold")
            self._update_mold_controls_for_source()
            self._refresh_mold_preview()

    def _update_image_controls_for_source(self) -> None:
        """Hide raster-only controls when an SVG is loaded."""
        if self._image_path and is_svg_path(self._image_path):
            self._raster_controls.grid_remove()
            self._image_hint.configure(
                text="SVG: vector paths + live text (system fonts). For exact letterforms, convert text to outlines in Inkscape/Illustrator."
            )
        else:
            self._raster_controls.grid()
            self._image_hint.configure(
                text="PNG/JPG: high-contrast silhouettes. SVG: vector paths + live text (convert text to outlines for exact fonts)."
            )

    def _update_mold_controls_for_source(self) -> None:
        if self._image_path and is_svg_path(self._image_path):
            self._mold_raster_controls.grid_remove()
        else:
            self._mold_raster_controls.grid()

    def _refresh_image_preview(self) -> None:
        if not self._image_path or self._mode.get() != "image":
            return
        try:
            settings = self._collect_image_settings()
            mask = preview_image_mask(settings)
            img = Image.fromarray(mask)
            img = ImageOps.invert(img)
            img.thumbnail((280, 140), Image.Resampling.LANCZOS)
            self._image_preview_ref = ctk.CTkImage(
                light_image=img, dark_image=img, size=img.size
            )
            self._image_preview_label.configure(image=self._image_preview_ref, text="")
        except StampGenerationError:
            self._image_preview_label.configure(
                image=None, text="Could not preview image"
            )

    def _refresh_mold_preview(self) -> None:
        if (
            self._mode.get() != "mold"
            or self._mold_shape.get() != SHAPE_SILHOUETTE
            or not self._image_path
        ):
            return
        try:
            settings = ImageStampSettings(
                image_path=self._image_path,
                width_mm=self._slider_value(self._mold_target_width),
                threshold=int(round(self._slider_value(self._mold_threshold))),
                invert=self._mold_invert_var.get(),
                simplify=self._slider_value(self._mold_simplify),
                raised_border=False,
            )
            mask = preview_image_mask(settings)
            img = Image.fromarray(mask)
            img = ImageOps.invert(img)
            img.thumbnail((280, 120), Image.Resampling.LANCZOS)
            self._mold_preview_ref = ctk.CTkImage(
                light_image=img, dark_image=img, size=img.size
            )
            self._mold_image_preview_label.configure(
                image=self._mold_preview_ref, text=""
            )
        except StampGenerationError:
            self._mold_image_preview_label.configure(
                image=None, text="Could not preview silhouette"
            )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self._preview_btn.configure(state=state)
        self._export_btn.configure(state=state)
        self._save_draft_btn.configure(state=state)
        self._open_draft_btn.configure(state=state)

    def _set_status(self, msg: str) -> None:
        self._status.configure(text=msg)

    def _on_preview(self) -> None:
        if self._busy:
            return
        self._run_generation(preview_only=True)

    def _on_export(self) -> None:
        if self._busy:
            return

        mode = self._mode.get()
        if mode == "mold":
            default_name = "valet_mold_male.stl"
            title = "Save mold STL"
        elif mode == "image":
            default_name = "leather_stamp_image.stl"
            title = "Save stamp STL"
        else:
            default_name = "leather_stamp.stl"
            title = "Save stamp STL"

        path = filedialog.asksaveasfilename(
            title=title,
            defaultextension=".stl",
            filetypes=[("STL mesh", "*.stl")],
            initialfile=default_name,
        )
        if not path:
            return
        self._run_generation(preview_only=False, export_path=path)

    def _snapshot_ui_settings(self) -> dict:
        return collect_settings_dict(
            mode=self._mode.get(),
            text=self._text.get("1.0", "end").strip(),
            font_family=self._font_var.get(),
            custom_font_path=self._custom_font_path,
            font_size=self._slider_value(self._font_size),
            imprint_depth=self._slider_value(self._imprint_depth),
            base_thickness=self._slider_value(self._base_thickness),
            margin=self._slider_value(self._margin),
            mirror_for_leather=self._mirror_var.get(),
            image_path=self._image_path,
            width_mm=self._slider_value(self._stamp_width),
            threshold=int(round(self._slider_value(self._threshold))),
            invert=self._invert_var.get(),
            simplify=self._slider_value(self._simplify),
            orientation=self._orientation.get(),
            raised_border=self._raised_border_var.get(),
            border_width=self._slider_value(self._border_width),
            mold_shape=self._mold_shape.get(),
            mold_length_mm=self._slider_value(self._mold_length),
            mold_width_mm=self._slider_value(self._mold_width),
            mold_height_mm=self._slider_value(self._mold_height),
            mold_corner_radius_mm=self._slider_value(self._mold_corner),
            mold_draft_deg=self._slider_value(self._mold_draft),
            mold_top_fillet_mm=self._slider_value(self._mold_fillet),
            mold_flange_mm=self._slider_value(self._mold_flange),
            mold_leather_thickness_mm=self._slider_value(self._mold_leather),
            mold_include_female=self._mold_female_var.get(),
            mold_target_width_mm=self._slider_value(self._mold_target_width),
            mold_threshold=int(round(self._slider_value(self._mold_threshold))),
            mold_invert=self._mold_invert_var.get(),
            mold_simplify=self._slider_value(self._mold_simplify),
        )

    def _on_save_draft(self) -> None:
        if self._busy:
            return

        path = filedialog.asksaveasfilename(
            title="Save stamp draft",
            defaultextension=".stldraft",
            filetypes=[("STL Builder draft", "*.stldraft")],
            initialfile="stamp_draft.stldraft",
        )
        if not path:
            return

        settings = self._snapshot_ui_settings()
        image_path = self._image_path
        custom_font_path = self._custom_font_path
        model = self._model

        if model is None:
            regenerate = messagebox.askyesno(
                "No preview yet",
                "No STL is generated yet. Build one and include it in the draft?",
            )
            if not regenerate:
                try:
                    save_draft(
                        path,
                        settings,
                        model=None,
                        image_path=image_path,
                        custom_font_path=custom_font_path,
                    )
                except Exception as exc:
                    messagebox.showerror("Save failed", str(exc))
                    return
                self._set_status(f"Draft saved (settings only): {path}")
                return

            self._set_busy(True)
            self._set_status("Generating geometry for draft…")

            def work_build_then_save() -> None:
                try:
                    mode = self._mode.get()
                    if mode == "image":
                        built = build_image_stamp(self._collect_image_settings())
                        female = None
                    elif mode == "mold":
                        result = build_valet_mold_set(self._collect_mold_settings())
                        built = result.male
                        female = result.female
                    else:
                        built = build_stamp(self._collect_text_settings())
                        female = None
                    out = save_draft(
                        path,
                        settings,
                        model=built,
                        image_path=image_path,
                        custom_font_path=custom_font_path,
                    )
                    self.after(
                        0,
                        lambda: self._on_draft_saved(
                            built, str(out), female_model=female
                        ),
                    )
                except StampGenerationError as exc:
                    self.after(0, lambda: self._on_error(str(exc)))
                except Exception as exc:
                    self.after(0, lambda: self._on_error(f"Could not save draft: {exc}"))

            threading.Thread(target=work_build_then_save, daemon=True).start()
            return

        self._set_busy(True)
        self._set_status("Saving draft…")

        def work_save() -> None:
            try:
                out = save_draft(
                    path,
                    settings,
                    model=model,
                    image_path=image_path,
                    custom_font_path=custom_font_path,
                )
                self.after(
                    0,
                    lambda: self._on_draft_saved(
                        model, str(out), female_model=self._female_model
                    ),
                )
            except Exception as exc:
                self.after(0, lambda: self._on_error(f"Could not save draft: {exc}"))

        threading.Thread(target=work_save, daemon=True).start()

    def _on_draft_saved(self, model, path: str, female_model=None) -> None:
        self._model = model
        self._female_model = female_model
        if hasattr(model, "vertices"):
            self._update_preview_from_mesh(model)
        else:
            self._update_preview_plot(model)
        self._set_busy(False)
        self._set_status(f"Draft saved: {path}")

    def _on_open_draft(self) -> None:
        if self._busy:
            return

        path = filedialog.askopenfilename(
            title="Open stamp draft",
            filetypes=[("STL Builder draft", "*.stldraft"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            draft = load_draft(path)
        except StampGenerationError as exc:
            messagebox.showerror("Open failed", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Open failed", f"Could not open draft:\n{exc}")
            return

        # Keep extracted assets alive for this session.
        self._draft_workspace = draft._workspace
        self._apply_draft(draft)
        self._set_status(f"Draft opened: {path}")

    def _apply_draft(self, draft) -> None:
        data = draft.settings
        shared = data.get("shared") or {}
        text = data.get("text") or {}
        image = data.get("image") or {}
        mold = data.get("mold") or {}

        self._set_slider(self._imprint_depth, shared.get("imprint_depth", 2.0))
        self._set_slider(self._base_thickness, shared.get("base_thickness", 5.0))
        self._set_slider(self._margin, shared.get("margin", 4.0))
        self._mirror_var.set(bool(shared.get("mirror_for_leather", True)))

        self._text.delete("1.0", "end")
        self._text.insert("1.0", text.get("text") or "")

        if draft.custom_font_path is not None:
            self._custom_font_path = str(draft.custom_font_path)
            self._custom_font_label.configure(
                text=f"Custom: {draft.custom_font_path.name}",
                text_color=("gray10", "gray90"),
            )
        else:
            self._custom_font_path = None
            self._custom_font_label.configure(
                text="No custom font loaded", text_color="gray"
            )
            family = text.get("font_family") or "Arial"
            font_names = [f.display_name for f in self._fonts]
            if family in font_names:
                self._font_var.set(family)

        self._set_slider(self._font_size, text.get("font_size", 12.0))
        orientation = text.get("orientation", "horizontal")
        if orientation not in ("horizontal", "vertical"):
            orientation = "horizontal"
        self._orientation.set(orientation)
        self._set_slider(self._stamp_width, image.get("width_mm", 40.0))
        self._set_slider(self._threshold, image.get("threshold", 128))
        self._set_slider(self._simplify, image.get("simplify", 0.0))
        self._invert_var.set(bool(image.get("invert", False)))
        self._raised_border_var.set(bool(image.get("raised_border", False)))
        self._set_slider(self._border_width, image.get("border_width", 1.5))

        shape = mold.get("shape", SHAPE_ROUNDED_RECT)
        if shape not in (SHAPE_RECT, SHAPE_ROUNDED_RECT, SHAPE_OVAL, SHAPE_SILHOUETTE):
            shape = SHAPE_ROUNDED_RECT
        self._mold_shape.set(shape)
        self._set_slider(self._mold_length, mold.get("length_mm", 180.0))
        self._set_slider(self._mold_width, mold.get("width_mm", 130.0))
        self._set_slider(self._mold_height, mold.get("height_mm", 18.0))
        self._set_slider(self._mold_corner, mold.get("corner_radius_mm", 12.0))
        self._set_slider(self._mold_draft, mold.get("draft_deg", 2.0))
        self._set_slider(self._mold_fillet, mold.get("top_fillet_mm", 2.0))
        self._set_slider(self._mold_flange, mold.get("flange_mm", 8.0))
        self._set_slider(self._mold_leather, mold.get("leather_thickness_mm", 3.2))
        self._mold_female_var.set(bool(mold.get("include_female", False)))
        self._set_slider(self._mold_target_width, mold.get("target_width_mm", 180.0))
        self._set_slider(self._mold_threshold, mold.get("threshold", 128))
        self._set_slider(self._mold_simplify, mold.get("simplify", 0.0))
        self._mold_invert_var.set(bool(mold.get("invert", False)))

        if draft.image_path is not None:
            self._image_path = str(draft.image_path)
            display_name = (
                image.get("original_image_name")
                or mold.get("original_image_name")
                or draft.image_path.name
            )
            self._image_name_label.configure(
                text=display_name, text_color=("gray10", "gray90")
            )
            self._mold_image_name_label.configure(
                text=display_name, text_color=("gray10", "gray90")
            )
        else:
            self._image_path = None
            self._image_name_label.configure(text="No image loaded", text_color="gray")
            self._image_preview_label.configure(image=None, text="Mask preview")
            self._image_preview_ref = None
            self._mold_image_name_label.configure(
                text="No silhouette loaded", text_color="gray"
            )
            self._mold_image_preview_label.configure(image=None, text="Outline preview")
            self._mold_preview_ref = None

        mode = draft.mode
        if mode == "image" and not self._image_path:
            mode = "text"
        if mode == "mold" and shape == SHAPE_SILHOUETTE and not self._image_path:
            mode = "mold"
            self._mold_shape.set(SHAPE_ROUNDED_RECT)
        self._mode.set(mode)
        self._on_mode_changed(mode)
        self._update_image_controls_for_source()

        self._female_model = None
        if draft.stl_path is not None:
            try:
                mesh = trimesh.load(str(draft.stl_path), force="mesh")
                self._model = mesh
                self._update_preview_from_mesh(mesh)
            except Exception:
                self._model = None
                self._set_status("Draft settings loaded; STL preview unavailable.")
        else:
            self._model = None

    def _update_preview_from_mesh(self, mesh) -> None:
        verts = np.asarray(mesh.vertices)
        faces = np.asarray(mesh.faces)
        if verts.size == 0 or faces.size == 0:
            self._set_status("Preview unavailable; mesh has no geometry.")
            return

        self._ax.clear()
        self._ax.plot_trisurf(
            verts[:, 0],
            verts[:, 1],
            verts[:, 2],
            triangles=faces,
            color="#4a90d9",
            edgecolor="none",
            linewidth=0.0,
            antialiased=True,
            alpha=0.95,
        )
        self._ax.set_xlabel("X (mm)")
        self._ax.set_ylabel("Y (mm)")
        self._ax.set_zlabel("Z (mm)")
        title = (
            "Valet mold (male plug)"
            if self._mode.get() == "mold"
            else "Stamp (top = raised design)"
        )
        self._ax.set_title(title)

        # Match real mm proportions (matplotlib 3D otherwise stretches each axis
        # to a cube, which makes a thin base look much larger than the STL).
        extents = np.ptp(verts, axis=0)
        extents = np.where(extents < 1e-9, 1.0, extents)
        self._ax.set_box_aspect(extents)

        mins = verts.min(axis=0)
        maxs = verts.max(axis=0)
        pad = 0.02 * extents
        self._ax.set_xlim(mins[0] - pad[0], maxs[0] + pad[0])
        self._ax.set_ylim(mins[1] - pad[1], maxs[1] + pad[1])
        self._ax.set_zlim(mins[2] - pad[2], maxs[2] + pad[2])

        self._azim = getattr(self, "_azim", 45)
        self._elev = getattr(self, "_elev", 28)
        self._ax.view_init(elev=self._elev, azim=self._azim)
        self._fig.tight_layout()
        self._canvas.draw_idle()

    def _export_mold_paths(self, export_path: str) -> tuple[str, str | None]:
        """Return male path and optional female path derived from the save dialog."""
        out = Path(export_path)
        stem = out.stem
        suffix = out.suffix or ".stl"
        parent = out.parent
        if stem.lower().endswith("_male"):
            male_path = str(out)
            female_path = str(parent / f"{stem[:-5]}_female{suffix}")
        elif stem.lower().endswith("_female"):
            base = stem[:-7]
            male_path = str(parent / f"{base}_male{suffix}")
            female_path = str(out)
        else:
            male_path = str(parent / f"{stem}_male{suffix}")
            female_path = str(parent / f"{stem}_female{suffix}")
        return male_path, female_path

    def _run_generation(self, preview_only: bool, export_path: str | None = None) -> None:
        try:
            mode = self._mode.get()
            if mode == "image":
                settings = self._collect_image_settings()
            elif mode == "mold":
                settings = self._collect_mold_settings()
            else:
                settings = self._collect_text_settings()
        except StampGenerationError as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        self._set_busy(True)
        self._set_status("Generating geometry…")

        def work() -> None:
            try:
                female = None
                if isinstance(settings, ImageStampSettings):
                    model = build_image_stamp(settings)
                    exported = export_path
                    if export_path:
                        export_stl(model, export_path)
                elif isinstance(settings, ValetMoldSettings):
                    result = build_valet_mold_set(settings)
                    model = result.male
                    female = result.female
                    if export_path:
                        male_path, female_path = self._export_mold_paths(export_path)
                        export_stl(model, male_path)
                        exported = male_path
                        if female is not None and female_path:
                            export_stl(female, female_path)
                            exported = f"{male_path} + {female_path}"
                    else:
                        exported = None
                else:
                    model = build_stamp(settings)
                    exported = export_path
                    if export_path:
                        export_stl(model, export_path)
                self.after(
                    0,
                    lambda: self._on_success(
                        model, exported, preview_only, female_model=female
                    ),
                )
            except StampGenerationError as exc:
                self.after(0, lambda: self._on_error(str(exc)))
            except Exception as exc:
                self.after(0, lambda: self._on_error(f"Unexpected error: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _on_success(
        self,
        model,
        export_path: str | None,
        preview_only: bool,
        female_model=None,
    ) -> None:
        self._model = model
        self._female_model = female_model
        self._update_preview_plot(model)
        self._set_busy(False)
        if export_path:
            self._set_status(f"Exported: {export_path}")
        elif preview_only:
            self._set_status("Preview updated.")

    def _on_error(self, msg: str) -> None:
        self._set_busy(False)
        self._set_status("Generation failed.")
        messagebox.showerror("Generation failed", msg)

    def _update_preview_plot(self, model) -> None:
        try:
            mesh = model_to_trimesh(model)
        except Exception:
            self._set_status("Preview unavailable; export may still work.")
            return
        self._update_preview_from_mesh(mesh)


def run_app() -> None:
    app = StampDesignerApp()
    app.mainloop()
