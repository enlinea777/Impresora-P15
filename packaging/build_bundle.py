#!/usr/bin/env python3
"""
build_bundle.py — paquete portable autocontenido de P15 Designer
================================================================
Genera una carpeta autocontenida (Python + bun + dependencias) y un archivo
comprimido listo para distribuir. Se ejecuta en el sistema operativo destino:

    python packaging/build_bundle.py

Salidas (en dist/):
    P15-Designer-<version>-linux-x86_64.tar.gz
    P15-Designer-<version>-linux-aarch64.tar.gz
    P15-Designer-<version>-windows-x86_64.zip
    P15-Designer-<version>-windows-aarch64.zip
    P15-Designer-<version>-macos-x86_64.zip     (contiene P15 Designer.app)
    P15-Designer-<version>-macos-aarch64.zip

Opciones:
    --no-archive     deja solo la carpeta build/bundle/P15-Designer
    --outdir DIR     directorio de salida para el archivo comprimido
    --version X.Y.Z  versión del paquete (o variable P15_VERSION)

Requisitos para compilar: Python 3.10+, curl/tar/zip no son necesarios (todo
se hace con la biblioteca estándar) y conexión a Internet la primera vez.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

# En Windows la consola usa cp1252 y falla con ▶/❌; forzar UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".build-cache"
BUILD = ROOT / "build"
BUNDLE_BUILD = BUILD / "bundle"

PY_VERSION = os.environ.get("P15_PY_VERSION", "3.12.16")
PBS_TAG = os.environ.get("P15_PBS_TAG", "20261110")
BUN_VERSION = os.environ.get("P15_BUN_VERSION", "1.4.5")

PBS_TARGETS = {
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("windows", "x86_64"): "x86_64-pc-windows-msvc",
    ("windows", "aarch64"): "aarch64-pc-windows-msvc",
    ("macos", "x86_64"): "x86_64-apple-darwin",
    ("macos", "aarch64"): "aarch64-apple-darwin",
}

BUN_TARGETS = {
    ("linux", "x86_64"): "bun-linux-x64.zip",
    ("linux", "aarch64"): "bun-linux-aarch64.zip",
    ("windows", "x86_64"): "bun-windows-x64.zip",
    ("windows", "aarch64"): "bun-windows-aarch64.zip",
    ("macos", "x86_64"): "bun-darwin-x64.zip",
    ("macos", "aarch64"): "bun-darwin-aarch64.zip",
}


def log(msg: str) -> None:
    print(f"\n\033[1;34m▶ {msg}\033[0m", flush=True)


def fail(msg: str) -> None:
    print(f"\n\033[1;31m❌ {msg}\033[0m", file=sys.stderr, flush=True)
    raise SystemExit(1)


def detect_target() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        os_name = "linux"
    elif system == "darwin":
        os_name = "macos"
    elif system == "windows":
        os_name = "windows"
    else:
        fail(f"Sistema operativo no soportado: {platform.system()}")

    arch = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(machine)
    if not arch:
        fail(f"Arquitectura no soportada: {machine}")
    if (os_name, arch) not in PBS_TARGETS:
        fail(f"No hay build de Python para {os_name}-{arch}")
    return os_name, arch


def download(url: str, dest: Path) -> Path:
    if dest.exists():
        print(f"   (en caché) {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"   Descargando {dest.name}…")
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "p15-designer-build"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    tmp.replace(dest)
    return dest


def copy_tree(src: Path, dst: Path, ignore_names: set[str]) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in ignore_names or n.endswith(".pyc")}

    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)


def write_launcher(path: Path, *, macos_app: bool = False) -> None:
    if macos_app:
        here = 'HERE="$(CDPATH= cd -- "$(dirname -- "$0")/../Resources/p15" && pwd)"'
    else:
        here = 'HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"'
    script = f"""#!/bin/sh
set -eu
{here}
export P15_APP_DIR="$HERE/app"
export P15_THERMOPRINT_DIR="$HERE/thermoprint"
export P15_ICON="$HERE/p15-designer.png"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
exec "$HERE/python/bin/python3" "$HERE/app/tray_app.py" "$@"
"""
    path.write_text(script, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_windows_launchers(bundle: Path) -> None:
    bat = r"""@echo off
setlocal
set "HERE=%~dp0"
set "P15_APP_DIR=%HERE%app"
set "P15_THERMOPRINT_DIR=%HERE%thermoprint"
set "P15_ICON=%HERE%p15-designer.png"
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
"%HERE%python\python.exe" "%HERE%app\tray_app.py" %*
"""
    (bundle / "P15-Designer.bat").write_text(bat.replace("\n", "\r\n"), encoding="utf-8")

    vbs = r''' ' Lanza P15 Designer sin ventana de consola
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Environment("PROCESS")("P15_APP_DIR") = dir & "\app"
sh.Environment("PROCESS")("P15_THERMOPRINT_DIR") = dir & "\thermoprint"
sh.Environment("PROCESS")("P15_ICON") = dir & "\p15-designer.png"
sh.Environment("PROCESS")("PYTHONNOUSERSITE") = "1"
sh.Environment("PROCESS")("PYTHONDONTWRITEBYTECODE") = "1"
q = Chr(34)
cmd = q & dir & "\python\pythonw.exe" & q & " " & q & dir & "\app\tray_app.py" & q
sh.Run cmd, 0, False
'''
    (bundle / "P15-Designer.vbs").write_text(vbs.replace("\n", "\r\n"), encoding="utf-8")


def compile_noble_win_arm64(thermoprint: Path) -> None:
    """Compila el addon nativo de noble para Windows ARM64 (no hay prebuild)."""
    log("Compilando @stoprocent/noble para Windows ARM64")
    candidates = list(
        (thermoprint / "node_modules" / ".bun").glob(
            "@stoprocent+noble@*/node_modules/@stoprocent/noble"
        )
    )
    if not candidates:
        fail("No se encontró el paquete @stoprocent/noble tras bun install")
    noble = candidates[0]

    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        fail("Se necesita Node/npx en el runner para compilar noble en Windows ARM64")

    env = os.environ.copy()
    env["npm_config_arch"] = "arm64"
    env["npm_config_target_arch"] = "arm64"
    # VS2026 marca /await y <experimental/coroutine> como obsoletos y emite
    # error; el macro oficial lo convierte en warning (noble aún usa /await).
    cl = env.get("CL", "")
    cl += " /D_SILENCE_EXPERIMENTAL_COROUTINE_DEPRECATION_WARNINGS"
    cl += " /D_SILENCE_ALL_CXX20_DEPRECATION_WARNINGS"
    env["CL"] = cl.strip()
    cmd = f'"{npx}" --yes node-gyp rebuild --arch=arm64'
    try:
        subprocess.run(cmd, cwd=noble, env=env, check=True, shell=True)
    except subprocess.CalledProcessError as e:
        fail(
            "Falló la compilación de noble para Windows ARM64. "
            "Revisa que el runner tenga Visual Studio con C++ ARM64.\n"
            f"   Comando: {cmd}\n   Error: {e}"
        )

    built = list((noble / "build" / "Release").glob("*.node"))
    if not built:
        fail("node-gyp no generó ningún .node para noble")
    print(f"   OK: {built[0].name}")


def make_macos_app(bundle: Path, version: str, out_zip: Path) -> None:
    app = BUNDLE_BUILD / "P15 Designer.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    shutil.copytree(bundle, resources / "p15")
    shutil.copy2(ROOT / "packaging" / "p15-designer.icns", resources / "p15-designer.icns")

    write_launcher(macos / "P15Designer", macos_app=True)
    (macos / "P15Designer").chmod(0o755)

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>P15 Designer</string>
  <key>CFBundleDisplayName</key><string>P15 Designer</string>
  <key>CFBundleIdentifier</key><string>io.github.enlinea777.ImpresoraP15</string>
  <key>CFBundleExecutable</key><string>P15Designer</string>
  <key>CFBundleIconFile</key><string>p15-designer.icns</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>{version}</string>
  <key>CFBundleVersion</key><string>{version}</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.graphics-design</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>MIT — thermoprint / P15 Designer</string>
</dict>
</plist>
"""
    (contents / "Info.plist").write_text(plist, encoding="utf-8")

    # Firma ad-hoc (sin certificado). Gatekeeper seguirá avisando al abrir.
    try:
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(app)],
            check=True,
        )
    except Exception as e:
        print(f"   ⚠ No se pudo firmar ad-hoc: {e}")

    if out_zip.exists():
        out_zip.unlink()
    try:
        subprocess.run(
            ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
             str(app), str(out_zip)],
            check=True,
        )
    except Exception:
        shutil.make_archive(str(out_zip.with_suffix("")), "zip",
                            root_dir=app.parent, base_dir=app.name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Empaqueta P15 Designer")
    parser.add_argument("--no-archive", action="store_true")
    parser.add_argument("--outdir", default=str(ROOT / "dist"))
    parser.add_argument("--version", default=os.environ.get("P15_VERSION", "1.0.0"))
    args = parser.parse_args()

    os_name, arch = detect_target()
    target = f"{os_name}-{arch}"
    version = args.version
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    BUNDLE_BUILD.mkdir(parents=True, exist_ok=True)

    bundle = BUNDLE_BUILD / "P15-Designer"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    # 1. Python standalone
    pbs_arch = PBS_TARGETS[(os_name, arch)]
    pbs_name = f"cpython-{PY_VERSION}+{PBS_TAG}-{pbs_arch}-install_only.tar.gz"
    pbs_url = (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{PBS_TAG}/{pbs_name}"
    )
    log(f"Python {PY_VERSION} ({pbs_arch})")
    pbs_tgz = download(pbs_url, CACHE / pbs_name)
    with tarfile.open(pbs_tgz, "r:gz") as tf:
        try:
            tf.extractall(bundle, filter="fully_trusted")
        except TypeError:  # Python < 3.12
            tf.extractall(bundle)

    if os_name == "windows":
        py = bundle / "python" / "python.exe"
    else:
        py = bundle / "python" / "bin" / "python3"
    if not py.exists():
        fail(f"No se encontró el intérprete en {py}")

    # 2. Dependencias Python
    log("Instalando dependencias de Python")
    req = ROOT / "packaging" / "requirements.lock.txt"
    req_build = BUNDLE_BUILD / "requirements.txt"
    req_text = req.read_text(encoding="utf-8")
    if os_name == "macos":
        req_text += "\npyobjc-framework-Cocoa>=10.3\n"
    req_build.write_text(req_text, encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    subprocess.run(
        [str(py), "-s", "-m", "pip", "install", "--disable-pip-version-check",
         "--no-warn-script-location", "-r", str(req_build)],
        check=True, env=env,
    )

    # 3. bun
    log(f"bun {BUN_VERSION} ({target})")
    bun_zip_name = BUN_TARGETS[(os_name, arch)]
    bun_zip = download(
        f"https://github.com/oven-sh/bun/releases/download/bun-v{BUN_VERSION}/{bun_zip_name}",
        CACHE / bun_zip_name,
    )
    bun_extract = BUNDLE_BUILD / "bun-extract"
    if bun_extract.exists():
        shutil.rmtree(bun_extract)
    with zipfile.ZipFile(bun_zip) as zf:
        zf.extractall(bun_extract)
    bun_src = next(bun_extract.rglob("bun.exe" if os_name == "windows" else "bun"), None)
    if not bun_src:
        fail("No se encontró el binario de bun en el zip")
    thermoprint = bundle / "thermoprint"
    thermoprint.mkdir(parents=True)
    bun_name = "bun.exe" if os_name == "windows" else "bun"
    bun_dst = thermoprint / bun_name
    shutil.copy2(bun_src, bun_dst)
    bun_dst.chmod(0o755)

    if os_name == "macos":
        # Evita el bloqueo de Gatekeeper/quarantine y firma ad-hoc.
        try:
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(bun_dst)],
                           check=False)
            subprocess.run(["codesign", "--force", "--sign", "-", str(bun_dst)],
                           check=False)
        except Exception:
            pass

    # 4. Código de la app
    log("Copiando código de la aplicación")
    app_dst = bundle / "app"
    app_dst.mkdir(parents=True)
    for item in ("app.py", "printer.py", "tray_app.py"):
        shutil.copy2(ROOT / item, app_dst / item)
    for item in ("templates", "static"):
        shutil.copytree(ROOT / item, app_dst / item)
    (app_dst / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    shutil.copy2(ROOT / "packaging" / "p15-designer.png", bundle / "p15-designer.png")

    # 5. Dependencias Node
    log("Instalando dependencias Node con bun")
    stage = BUNDLE_BUILD / f"thermoprint-{target}"
    if stage.exists():
        shutil.rmtree(stage)
    copy_tree(
        ROOT / "thermoprint_ref",
        stage,
        ignore_names={"node_modules", ".git", "__pycache__"},
    )
    subprocess.run(
        [str(bun_dst), "install", "--production", "--ignore-scripts",
         "--filter", "@thermoprint/cli", "--frozen-lockfile"],
        cwd=stage, check=True,
    )
    # symlinks=True conserva los enlaces internos que crea bun (.bun/...).
    shutil.copytree(
        stage, thermoprint,
        dirs_exist_ok=True,
        symlinks=(os_name != "windows"),
    )

    if os_name == "windows" and arch == "aarch64":
        compile_noble_win_arm64(thermoprint)

    # 6. Lanzadores
    if os_name == "windows":
        write_windows_launchers(bundle)
    else:
        write_launcher(bundle / "P15-Designer")

    # 7. Empaquetar
    base = f"P15-Designer-{version}-{target}"
    if os_name == "macos":
        out_file = outdir / f"{base}.zip"
        log("Creando P15 Designer.app")
        make_macos_app(bundle, version, out_file)
    elif os_name == "windows":
        out_file = outdir / f"{base}.zip"
        log("Creando ZIP")
        if out_file.exists():
            out_file.unlink()
        shutil.make_archive(str(out_file.with_suffix("")), "zip",
                            root_dir=bundle.parent, base_dir=bundle.name)
    else:
        out_file = outdir / f"{base}.tar.gz"
        log("Creando tar.gz")
        if out_file.exists():
            out_file.unlink()
        with tarfile.open(out_file, "w:gz") as tf:
            tf.add(bundle, arcname=bundle.name)

    if not args.no_archive:
        import hashlib
        digest = hashlib.sha256(out_file.read_bytes()).hexdigest()
        (out_file.with_suffix(out_file.suffix + ".sha256")).write_text(
            f"{digest}  {out_file.name}\n", encoding="utf-8"
        )
        log("Listo")
        print(f"   {out_file}\n   {out_file.stat().st_size / 1024 / 1024:.1f} MB")
        print(f"   sha256: {digest}")
    else:
        log("Listo (sin archivo comprimido)")
        print(f"   {bundle}")


if __name__ == "__main__":
    main()
