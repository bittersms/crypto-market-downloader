# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Crypto Market Data Downloader (Tkinter GUI).
#
# Build:  pyinstaller cmd_exe.spec --noconfirm --clean
# Output: dist/CryptoMarketDownloader.exe  (standalone, windowed, x64)
#
# One-file onefreeze build. Notable points:
#   * launcher.py is the entry point; app.py holds the GUI.
#   * sources/*.py are imported by name via importlib.import_module() in
#     source_registry.py, so PyInstaller's static analysis never sees them.
#     They are declared as hiddenimports instead of raw data files so the
#     byte-compiled modules are packaged and importable from the bundle.
#   * requests[socks] pulls in `socks` (PySocks) only when a socks4/socks5
#     proxy URL is used. There is no PyInstaller hook for PySocks, so it is
#     listed explicitly to keep SOCKS proxying working in the frozen app.
#   * When adding a new exchange under sources/, append its module here too
#     or it will import fine from source but fail inside the frozen build.
#
# Frozen runtime state (settings.json, config.db) is written next to the exe
# unless CMD_APP_DIR is set in the environment.

block_cipher = None

hiddenimports = [
    'app',
    'gui',
    'widgets',
    'config',
    'database',
    'failover',
    'proxy_manager',
    'source_registry',
    'data_fetcher',
    'mt5_exporter',
    'api_server',
    'progress',

    'sources',
    'sources.binance',
    'sources.binance_like',
    'sources.binance_like_extras',
    'sources.coingecko',
    'sources.gate',
    'sources.hyperliquid',
    'sources.kucoin',
    'sources.lbank',
    'sources.mexc',
    'sources.native_exchanges',
    'sources.okx',

    # requests[socks] support
    'socks',
]

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'numpy',
        'pandas',
        'PIL',
        'pytest',
        'unittest',
        'pydoc',
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
    name='CryptoMarketDownloader',
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='CryptoMarketDownloader',
)
