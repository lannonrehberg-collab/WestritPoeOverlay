# PyInstaller spec for the Windows build (onefile).
# Build: pyinstaller aldur-appraiser.spec
#
# RapidOCR ships its ONNX models + config.yaml as package data, and onnxruntime
# ships native libs; collect_all grabs both. config.toml and the detection
# template are bundled so resource_path() finds them under sys._MEIPASS.
# winsdk приносит проекции Windows.Media.Ocr — без них в собранном файле не
# работает распознавание русского текста (импорт ленивый, сам PyInstaller его
# не увидит).

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("rapidocr_onnxruntime", "onnxruntime", "winsdk"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# our bundled resources (src-relative paths preserved in the bundle)
datas += [
    ("config.toml", "."),
    ("assets/templates", "assets/templates"),   # шаблоны панели и крестика
    ("assets/lang", "assets/lang"),             # словарь RU→EN и id обмена
    ("assets/icon.png", "assets"),              # значок трея
    ("assets/icon.ico", "assets"),              # значок Windows
]

# These are imported lazily (inside functions) so PyInstaller's static analysis
# misses them; declaring them as hidden imports pulls them in (and triggers the
# PySide6 hook, which bundles the Qt runtime + platform plugins).
hiddenimports += [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "mss",
]

block_cipher = None

a = Analysis(
    ["scripts/pyinstaller_ru_overlay.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],  # not used; keeps the bundle smaller
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WestritPoeOverlay",
    icon="assets/icon.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,  # tray app: no console window. Errors surface via a tray notification.
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
