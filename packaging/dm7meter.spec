# PyInstaller spec: one-file console app bundling the web UI and the calibration table.
# Build:  pyinstaller packaging/dm7meter.spec   (run from the repository root)
from PyInstaller.utils.hooks import collect_submodules
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))

hidden = (
    collect_submodules("uvicorn")          # loops / protocols are imported by string name
    + collect_submodules("websockets")
    + ["psutil"]
)

a = Analysis(
    [os.path.join(ROOT, "bridge", "server.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "web"), "web"),
        (os.path.join(ROOT, "tools", "dm7_levelwt_table.json"), "tools"),
    ],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="dm7-external-meter",
    debug=False,
    strip=False,
    upx=False,
    console=True,      # keep the console: users see the URL / errors and can stop with Ctrl+C
    disable_windowed_traceback=False,
    target_arch=None,
)
