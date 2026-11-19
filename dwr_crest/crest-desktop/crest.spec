# -*- mode: python ; coding: utf-8 -*-
# Build:  pyinstaller crest.spec   (Python 3.10–3.12; crest/web/ is committed, rebuild it with `npm run build:desktop` after changing src/)
# Result: dist/Crest/ (Windows: dist\Crest\Crest.exe), and dist/Crest.app on macOS.

import sys
from pathlib import Path

if not Path("crest/web/index.html").is_file():
    raise SystemExit("crest/web/index.html is missing: run `npm run build:desktop` from the repo root first.")

block_cipher = None

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=[("crest/web", "crest/web")],
    hiddenimports=["crest", "crest.__main__", "crest.handoff", "crest.qgis_run", "crest.layers"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Crest",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Crest",
)
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Crest.app",
        icon=None,
        bundle_identifier="nd.dwr.crest",
        info_plist={"CFBundleName": "Crest", "CFBundleDisplayName": "Crest — Dam Breach Desk"},
    )
