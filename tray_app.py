"""
tray_app.py — Lanzador con ícono de bandeja del sistema (system tray)
=====================================================================
Arranca el servidor Flask en segundo plano y muestra un ícono en
la bandeja del sistema desde donde se puede abrir el navegador o
detener el servidor.

Uso:
    python tray_app.py [--port 5000] [--no-browser]
"""

import argparse
import io
import logging
import os
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

# ── pystray + PIL ──────────────────────────────────────────────────────────────
try:
    import pystray
    from PIL import Image as PILImage, ImageDraw
    _HAS_TRAY = True
except ImportError:
    pystray = None
    _HAS_TRAY = False
    try:
        from PIL import Image as PILImage, ImageDraw
    except ImportError:
        PILImage = ImageDraw = None


# ── Directorio de datos ───────────────────────────────────────────────────────
def _data_dir() -> Path:
    if env := os.environ.get("P15_DATA_DIR"):
        return Path(env)
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
        return Path(base) / "P15-Designer"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/P15-Designer"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "p15-designer"


# ── Logging ───────────────────────────────────────────────────────────────────
_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _log_dir = _data_dir()
    _log_dir.mkdir(parents=True, exist_ok=True)
    _handlers.append(logging.FileHandler(_log_dir / "p15.log", encoding="utf-8"))
except Exception:
    pass  # sin permisos para escribir el log → solo consola

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────
DEFAULT_PORT = int(os.environ.get("P15_PORT", "5000"))
_server_thread: threading.Thread | None = None
_tray_icon: pystray.Icon | None = None
_port: int = DEFAULT_PORT


# ── Crear ícono de impresora 64×64 ────────────────────────────────────────────
def _make_icon():
    """Usa el ícono del AppImage si existe; si no, dibuja una impresora."""
    icon_path = os.environ.get("P15_ICON")
    if icon_path and Path(icon_path).exists() and PILImage is not None:
        return PILImage.open(icon_path).convert("RGBA")
    if PILImage is None or ImageDraw is None:
        return None

    SIZE = 64
    img  = PILImage.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d    = ImageDraw.Draw(img)

    # Cuerpo de la impresora (rectángulo oscuro)
    BG = (30, 120, 200, 255)     # azul
    d.rounded_rectangle([8, 22, 56, 46], radius=5, fill=BG)

    # Papel entrando por arriba
    d.rectangle([20, 10, 44, 26], fill=(240, 240, 240, 255))

    # Papel saliendo por abajo
    d.rectangle([20, 38, 44, 56], fill=(240, 240, 240, 255))
    # líneas de texto en el papel
    for y in (42, 47, 52):
        d.rectangle([24, y, 40, y + 2], fill=(180, 180, 180, 255))

    # Indicador LED verde
    d.ellipse([44, 26, 52, 34], fill=(50, 220, 50, 255))

    return img


# ── Lanzar servidor Flask ─────────────────────────────────────────────────────
def _start_server(port: int, open_browser: bool) -> threading.Thread:
    """Importa app y arranca Flask en un hilo daemon."""
    sys.path.insert(0, str(Path(__file__).parent))
    from app import run_server

    t = threading.Thread(
        target=run_server,
        kwargs={"port": port, "open_browser": open_browser},
        daemon=True,
        name="flask-server",
    )
    t.start()
    return t


# ── Acciones del menú ─────────────────────────────────────────────────────────
def _on_open(icon, item):
    webbrowser.open(f"http://localhost:{_port}")


def _shutdown() -> None:
    """Detiene el servidor BLE y termina el proceso."""
    try:
        import printer
        printer._stop_ble_server()
    except Exception:
        pass
    os._exit(0)


def _on_quit(icon, item):
    log.info("Deteniendo servidor…")
    icon.stop()
    # El hilo de Flask es daemon → termina solo al salir del proceso
    _shutdown()


# ── Utilidades ────────────────────────────────────────────────────────────────
def _already_running(port: int) -> bool:
    """True si ya hay una instancia de P15 atendiendo en ese puerto."""
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/api/label_info", timeout=1.5
        ) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_forever() -> None:
    """Modo sin bandeja: mantiene el servidor vivo hasta Ctrl+C/SIGTERM."""
    log.info("Servidor activo en http://localhost:%d — Ctrl+C para detener.", _port)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Ctrl+C recibido, deteniendo…")
        _shutdown()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _tray_icon, _server_thread, _port

    parser = argparse.ArgumentParser(description="Impresora P15 — bandeja del sistema")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="No abrir el navegador al iniciar")
    parser.add_argument("--no-tray", action="store_true", help="No mostrar ícono de bandeja")
    args = parser.parse_args()
    _port = args.port

    # Limpieza ordenada al recibir SIGINT/SIGTERM (cierra bun/ble_server).
    def _handle_signal(sig, frame):
        log.info("Señal %s recibida, deteniendo…", sig)
        _shutdown()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Si ya hay una instancia, solo abrimos el navegador (evita EADDRINUSE).
    if _already_running(_port):
        log.info("Ya hay una instancia en el puerto %d; abriendo el navegador.", _port)
        if not args.no_browser:
            webbrowser.open(f"http://localhost:{_port}")
        return

    log.info("Iniciando servidor en el puerto %d…", _port)
    _server_thread = _start_server(_port, open_browser=not args.no_browser)

    # Pequeña pausa para que el servidor esté listo antes de mostrar el ícono
    time.sleep(0.8)

    if args.no_tray or not _HAS_TRAY or _make_icon() is None:
        if not _HAS_TRAY:
            log.warning("pystray no está disponible; se ejecuta solo el servidor web.")
        _wait_forever()
        return

    menu = pystray.Menu(
        pystray.MenuItem("Abrir en navegador", _on_open, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Detener servidor", _on_quit),
    )

    _tray_icon = pystray.Icon(
        name="impresora-p15",
        icon=_make_icon(),
        title=f"Impresora P15  ·  puerto {_port}",
        menu=menu,
    )

    log.info("Ícono en bandeja listo. Clic derecho para el menú. Ctrl+C para detener.")
    try:
        _tray_icon.run()  # bloquea hasta _on_quit → os._exit()
    except KeyboardInterrupt:
        log.info("Ctrl+C recibido, deteniendo…")
        _shutdown()
    except Exception as e:
        # Wayland sin XWayland, entorno sin bandeja, etc.
        log.warning("No se pudo usar la bandeja del sistema (%s); usando modo web.", e)
        _wait_forever()


if __name__ == "__main__":
    main()
