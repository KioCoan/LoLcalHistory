# PyInstaller build for LoLcal History.
#   .venv/Scripts/pyinstaller.exe lolhist.spec --noconfirm
#
# One file, no console. Data lives in %LOCALAPPDATA%\LoLcal History, not beside
# the executable — a one-file build unpacks to a temporary directory that is
# deleted on exit (see lolhist/config.py).

from PyInstaller.utils.hooks import collect_submodules

# Package resources. The paths mirror the source layout so `Path(__file__).parent`
# resolves the same way frozen as it does from a checkout.
datas = [
    ("lolhist/schema.sql", "lolhist"),
    ("lolhist/web/templates", "lolhist/web/templates"),
    ("lolhist/assets", "lolhist/assets"),
]

hiddenimports = [
    # pywebview picks its backend at runtime, so the Windows one is never seen
    # by the dependency analyser.
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader",
    "pythonnet",
    # Same story for the tray backend.
    "pystray._win32",
    # Imported lazily by the CLI and the watcher.
    "lolhist.desktop",
    "lolhist.watcher",
    "lolhist.web.app",
] + collect_submodules("werkzeug")

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Trim the parts of the scientific stack Pillow can drag in.
    excludes=["tkinter", "matplotlib", "numpy", "pytest", "PySide6", "PyQt5", "PyQt6"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LoLcal History",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # windowed app; errors go to data/app.log
    icon="lolhist/assets/icon.ico",
)
