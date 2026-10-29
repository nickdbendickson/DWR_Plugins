# -*- mode: python ; coding: utf-8 -*-
# Build on macOS:  pyinstaller crest.spec
# Result: dist/Crest.app

import sys

block_cipher = None

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=[("crest/ui/index.html", "crest/ui")],
    hiddenimports=["crest", "crest.engine", "crest.export", "crest.samples"],
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
