# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

hidden_imports = []

for pkg in [
    'akshare', 'tushare', 'pandas', 'numpy', 'requests',
    'rich', 'openpyxl', 'lxml', 'html5lib', 'beautifulsoup4',
    'bs4', 'jsonpath', 'tqdm', 'urllib3', 'certifi',
    'charset_normalizer', 'idna', 'tabulate', 'xlrd',
    'decorator', 'simplejson', 'websocket',
    'markdown_it_py', 'pygments', 'et_xmlfile',
    'dateutil', 'tzdata', 'pytz',
    'curl_cffi', 'mini_racer', 'yfinance',
    'colorama', 'cffi', 'soupsieve', 'pycparser',
    'multitasking', 'peewee', 'platformdirs', 'websockets',
]:
    try:
        subs = collect_submodules(pkg)
        hidden_imports.extend(subs)
    except Exception:
        hidden_imports.append(pkg)

datas = []
for pkg_name in ['curl_cffi', 'certifi', 'tzdata', 'akshare']:
    try:
        pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg_name)
        datas.extend(pkg_datas)
        hidden_imports.extend(pkg_hiddenimports)
    except Exception:
        pass

import akshare as _ak
_ak_dir = os.path.dirname(_ak.__file__)
_ff = os.path.join(_ak_dir, 'file_fold')
if os.path.isdir(_ff):
    datas.append((_ff, 'akshare/file_fold'))

a = Analysis(
    ['Akshare.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'scipy', 'IPython', 'jupyter',
        'notebook', 'PIL', 'tkinter', 'PyQt5', 'PyQt6',
        'PySide2', 'PySide6', 'wx', 'win32ui', 'win32con',
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Akshare',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
