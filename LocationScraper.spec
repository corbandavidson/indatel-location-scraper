# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — builds a tiny launcher exe.
Only bundles desktop.py + stdlib. All heavy packages
live in the embedded Python folder alongside the exe.
"""

import os

block_cipher = None

a = Analysis(
    ["desktop.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "streamlit", "pandas", "openpyxl", "beautifulsoup4", "bs4",
        "lxml", "requests", "httpx", "playwright", "tenacity",
        "fake_useragent", "usaddress", "geopy", "duckduckgo_search",
        "numpy", "scipy", "matplotlib", "PIL", "webview",
    ],
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
    name="LocationScraper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="logo.ico" if os.path.exists("logo.ico") else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="LocationScraper",
)
