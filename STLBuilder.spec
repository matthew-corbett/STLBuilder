# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for STL Builder (Windows onedir bundle)."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

ROOT = Path(SPECPATH)
icon_path = ROOT / "installer" / "app.ico"

casadi_datas, casadi_binaries, casadi_hiddenimports = collect_all("casadi")
ocp_datas, ocp_binaries, ocp_hiddenimports = collect_all("OCP")
cadquery_datas, cadquery_binaries, cadquery_hiddenimports = collect_all("cadquery")
ctk_datas = collect_data_files("customtkinter")
mpl_datas = collect_data_files("matplotlib")

hiddenimports = [
    "OCP",
    "cadquery",
    "cadquery.exporters",
    "cadquery.occ_impl",
    "cadquery.occ_impl.exporters",
    "matplotlib.backends.backend_tkagg",
    "PIL._tkinter_finder",
    "cv2",
    "shapely",
    "shapely.geometry",
    "shapely.validation",
    "trimesh",
    "numpy",
    *casadi_hiddenimports,
    *ocp_hiddenimports,
    *cadquery_hiddenimports,
]

datas = [
    *casadi_datas,
    *ocp_datas,
    *cadquery_datas,
    *ctk_datas,
    *mpl_datas,
]

binaries = [
    *casadi_binaries,
    *ocp_binaries,
    *cadquery_binaries,
]

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="STLBuilder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="STLBuilder",
)
