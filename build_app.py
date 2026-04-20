#!/usr/bin/env python3
"""
build_app.py — Empaquetador cross-platform con PyInstaller
==========================================================
Genera un ejecutable autocontenido de P15 Designer.

Uso:
    # Desde el directorio del proyecto, con el venv activo:
    python build_app.py

    # O sin activar el venv:
    venv/bin/python build_app.py   (Linux/Mac)
    venv\\Scripts\\python build_app.py   (Windows)

Salida:
    dist/P15_Designer            (Linux/Mac)
    dist/P15_Designer.exe        (Windows)
"""

import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
VENV = ROOT / "venv"


def pip_install(pkg: str):
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", pkg],
        stdout=subprocess.DEVNULL,
    )


def main():
    # 1. Asegurarse de que PyInstaller esté instalado en el venv
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Instalando PyInstaller en el venv…")
        pip_install("pyinstaller")

    # 2. Detectar plataforma
    system = platform.system()  # 'Linux', 'Darwin', 'Windows'
    is_win = system == "Windows"
    print(f"Empaquetando para: {system}")

    # 3. Separador de datos según plataforma
    sep = ";" if is_win else ":"

    # 4. Construir el comando PyInstaller
    name = "P15_Designer"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", name,
        # Incluir carpetas de recursos
        f"--add-data=templates{sep}templates",
        # static puede no existir aún; incluir solo si existe
        *(
            [f"--add-data=static{sep}static"]
            if (ROOT / "static").exists() else []
        ),
        *(
            [f"--add-data=uploads{sep}uploads"]
            if (ROOT / "uploads").exists() else []
        ),
        # Ocultar consola en Windows (para distribución)
        *(["--noconsole"] if is_win else []),
        # Icono (opcional — agregar un .ico en el directorio si se desea)
        # *(["--icon=icon.ico"] if is_win and (ROOT/"icon.ico").exists() else []),
        # Hidden imports que bleak puede necesitar
        "--hidden-import=bleak",
        "--hidden-import=bleak.backends.bluezdbus.client",
        "--hidden-import=bleak.backends.bluezdbus.scanner",
        "--hidden-import=bleak.backends.winrt.client",
        "--hidden-import=bleak.backends.winrt.scanner",
        "--hidden-import=bleak.backends.corebluetooth.client",
        "--hidden-import=bleak.backends.corebluetooth.scanner",
        "--hidden-import=qrcode",
        "--hidden-import=flask",
        "--hidden-import=PIL",
        # Punto de entrada
        "app.py",
    ]

    print("Ejecutando PyInstaller…")
    print(" ".join(cmd))
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("\n❌ Error al empaquetar.")
        sys.exit(result.returncode)

    exe = ROOT / "dist" / (name + (".exe" if is_win else ""))
    print(f"\n✅ Ejecutable generado: {exe}")
    print(f"   Tamaño: {exe.stat().st_size / 1024 / 1024:.1f} MB")
    print(
        "\nPara distribuir basta con copiar ese único archivo.\n"
        "Al ejecutarlo, abre automáticamente el servidor en http://localhost:5000"
    )


if __name__ == "__main__":
    main()
