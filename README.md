# STL Builder — Leather Stamp Designer

Desktop app for designing **3D-printable leather stamps**. Create raised-letter stamp faces with customizable text, font, size, and imprint depth, then export **`.stl`** files for your slicer.

## Features

- **Text** — single or multi-line names and sayings
- **Image / SVG** — import PNG/JPG silhouettes or SVG vector paths and extrude them as raised relief
- **Font** — any installed Windows font, or load a custom `.ttf`
- **Size** — letter height in millimeters
- **Imprint depth** — how tall the raised letters are (how deep they press into leather)
- **Base** — plate thickness and border margin around the text
- **Mirror** — flip text so the leather impression reads correctly
- **3D preview** — rotate-friendly view before export
- **STL export** — ready for FDM/resin printing

## Requirements

- Windows 10/11 (tested)
- Python 3.11+

## Install

```powershell
cd STLBuilder
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
python main.py
```

Or double-click `run.bat`.

## Build executable and installer (Windows)

Creates a standalone app at `dist\STLBuilder\STLBuilder.exe` and, if [Inno Setup 6](https://jrsoftware.org/isdl.php) is installed, a setup wizard at `dist\STLBuilder-Setup-1.0.0.exe`.

```powershell
.\build.ps1
```

Skip the installer and only build the `.exe` folder:

```powershell
.\build.ps1 -SkipInstaller
```

Distribute either the whole `dist\STLBuilder` folder (portable) or the `STLBuilder-Setup-*.exe` installer.

## Usage

### Text stamp

1. Choose **text** mode and enter your stamp text (use **Enter** for a second line).
2. Pick **horizontal** or **vertical** text direction (vertical stacks each letter top-to-bottom).
3. Pick a **font** and adjust **font size**, **imprint depth**, **base thickness**, and **margin**.
4. Leave **Mirror design** enabled so the stamped leather reads normally.
5. Click **Update Preview**, then **Export STL…**.

### Image / SVG stamp

1. Choose **image** mode and click **Import image / SVG…** (PNG, JPG, BMP, GIF, WebP, or **SVG**).
2. For **raster** images: adjust **stamp width**, **threshold**, and **edge simplify (%)**. For **SVG**: paths are imported as vectors (threshold/simplify are hidden).
3. Use **Invert** if you need light areas / background raised instead of the filled shapes.
4. Enable **Raised border around image** to add a rectangular frame outside the artwork (adjust **border width** as needed).
5. Set **imprint depth**, **base**, and **margin**, then preview and export.

SVG tip: convert text to outlines (paths) before importing — live `<text>` elements are skipped.

Print with the **raised design facing up**. The flat back of the base sits on your press or mallet.

### Drafts

Use **Save Draft…** / **Open Draft…** to store a `.stldraft` package with:

- All stamp settings (text or image mode)
- The current preview STL (if generated)
- The source image and/or custom `.ttf` (embedded so the draft travels with its assets)

Reopen a draft later to restore settings and the 3D preview without rebuilding from scratch.

## Tips for leather stamping

- Start with **2–3 mm** imprint depth; increase if you need a deeper impression.
- Use a sturdy base (**5 mm+**) for small stamps; thicker bases handle mallet strikes better.
- Sand the stamp face lightly after printing for a cleaner release from leather.
- Test on scrap leather before your final piece.

## Project layout

```
STLBuilder/
├── main.py                 # Launch the app
├── requirements.txt
└── stlbuilder/
    ├── stamp_generator.py  # Text geometry + STL export
    ├── image_stamp.py      # Image / SVG silhouette → STL
    ├── svg_stamp.py        # SVG vector path parsing
    ├── draft.py            # .stldraft save/load
    ├── geometry_utils.py   # Shared base plate helpers
    ├── fonts.py            # System font listing
    └── gui/
        └── app.py          # CustomTkinter UI
```
