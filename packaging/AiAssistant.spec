# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from importlib.util import find_spec
from PyInstaller.utils.hooks import collect_submodules


repository_root = Path(SPECPATH).resolve().parent
source_root = repository_root / "src"
entrypoint = source_root / "desktop_assistant" / "gui" / "__main__.py"

# PySide can be built with a newer MSVC runtime than the selected Python build.
# Put Qt's matching redistributable DLLs at the application load root so Python
# cannot preload an older copy before QtCore is imported.
pyside_spec = find_spec("PySide6")
if pyside_spec is None or pyside_spec.origin is None:
    raise RuntimeError("PySide6 must be installed before packaging.")
pyside_root = Path(pyside_spec.origin).resolve().parent
msvc_runtime_names = (
    "concrt140.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_codecvt_ids.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
)
msvc_runtime_binaries = [
    (str(pyside_root / name), ".")
    for name in msvc_runtime_names
    if (pyside_root / name).is_file()
]

hidden_imports = [
    "desktop_assistant.intent.openai_provider",
    "desktop_assistant.voice.openai_audio",
    "PySide6.QtMultimedia",
    "PySide6.QtNetwork",
]

# Generate UIAutomationClient's COM typelib wrappers during packaging. The
# editor resolver also generates them lazily for non-frozen development runs.
import comtypes.client
comtypes.client.GetModule("UIAutomationCore.dll")
hidden_imports.extend(collect_submodules("comtypes.gen"))

analysis = Analysis(
    [str(entrypoint)],
    pathex=[str(source_root)],
    binaries=msvc_runtime_binaries,
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "tkinter"],
    noarchive=False,
    optimize=0,
)

# Qt uses the Windows system ICU shim. Do not freeze unrelated ICU/OpenSSL DLLs
# that happen to be present on a developer's PATH (for example from document
# tooling); they can shadow the supported Windows/Python runtime copies.
ambient_dlls = {
    "icudt78.dll",
    "icuuc.dll",
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
}
analysis.binaries = type(analysis.binaries)(
    entry for entry in analysis.binaries if entry[0].casefold() not in ambient_dlls
)
pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="AiAssistant",
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

bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AiAssistant",
)
