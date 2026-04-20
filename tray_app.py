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
except ImportError:
    print("Faltan dependencias: pip install pystray pillow")
    sys.exit(1)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────
DEFAULT_PORT = 5000
_server_thread: threading.Thread | None = None
_tray_icon: pystray.Icon | None = None
_port: int = DEFAULT_PORT


# ── Crear ícono de impresora 64×64 ────────────────────────────────────────────
def _make_icon() -> PILImage.Image:
    """Dibuja un ícono simple de impresora (64×64 RGBA)."""
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


def _on_quit(icon, item):
    log.info("Deteniendo servidor…")
    icon.stop()
    # El hilo de Flask es daemon → termina solo al salir del proceso
    os._exit(0)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _tray_icon, _server_thread, _port

    parser = argparse.ArgumentParser(description="Impresora P15 — bandeja del sistema")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="No abrir el navegador al iniciar")
    args = parser.parse_args()
    _port = args.port

    log.info("Iniciando servidor en el puerto %d…", _port)
    _server_thread = _start_server(_port, open_browser=not args.no_browser)

    # Pequeña pausa para que el servidor esté listo antes de mostrar el ícono
    time.sleep(0.8)

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
        os._exit(0)


if __name__ == "__main__":
    main()
