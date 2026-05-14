# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — builds the tiny launcher exe for the AI edition.
Streamlit, google-genai and the scraper code all live in the embedded
Python folder alongside the exe.
"""

import os

block_cipher = None

a = Analysis(
    ["desktop_ai.py"],
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
        "fake_useragent", "usaddress", "geopy", "ddgs",
        "numpy", "scipy", "matplotlib", "PIL", "webview",
        "google",
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
    name="LocationScraperAI",
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
    name="LocationScraperAI",
)
