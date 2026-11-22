"""
printer.py — Backend de impresión P15
======================================
Habla con ble_server.ts (http://localhost:7315) que mantiene la conexión
BLE persistente a la impresora.

ble_server.ts se inicia automáticamente al importar este módulo y se
detiene al salir del proceso Python.
"""

import io
import json
import logging
import os
import platform
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)

# ── Sistema operativo ──────────────────────────────────────────────────────
_OS = platform.system()   # 'Linux', 'Darwin', 'Windows'

# ── Rutas ──────────────────────────────────────────────────────────────────
_BASE_DIR        = Path(__file__).parent
_THERMOPRINT_REF = _BASE_DIR / "thermoprint_ref"
_BLE_SERVER_TS   = _THERMOPRINT_REF / "packages" / "cli" / "src" / "ble_server.ts"
_THERMOPRINT_CLI = _THERMOPRINT_REF / "packages" / "cli" / "src" / "index.ts"

BLE_SERVER_PORT  = 7315
BLE_SERVER_URL   = f"http://localhost:{BLE_SERVER_PORT}"

# ── Buscar bun ─────────────────────────────────────────────────────────────
def _find_bun() -> str:
    if env := os.environ.get("BUN_BIN"):
        return env
    if found := shutil.which("bun"):
        return found
    candidates = []
    if _OS == "Windows":
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            Path(appdata) / "bun" / "bin" / "bun.exe",
            Path(localappdata) / "bun" / "bin" / "bun.exe",
        ]
    elif _OS == "Darwin":
        candidates = [Path.home() / ".bun/bin/bun", Path("/usr/local/bin/bun"), Path("/opt/homebrew/bin/bun")]
    else:
        candidates = [Path.home() / ".bun/bin/bun", Path("/usr/local/bin/bun")]
    for c in candidates:
        if c.exists():
            return str(c)
    return "bun"

_BUN = _find_bun()

# ── Constantes ─────────────────────────────────────────────────────────────
HEAD_W_PX          = 384   # ancho físico del cabezal (referencia, no se usa para padding)
DOTS_PER_MM        = 8
DEFAULT_LABEL_W_MM = 40    # etiqueta 40mm × 12mm
DEFAULT_LABEL_H_MM = 12

# ── Proceso del servidor BLE ───────────────────────────────────────────────
_ble_proc: Optional[subprocess.Popen] = None
_ble_lock = threading.Lock()
_shuttingDown = False

def _start_ble_server() -> None:
    """Arranca ble_server.ts en segundo plano si no está corriendo."""
    global _ble_proc
    with _ble_lock:
        if _ble_proc is not None and _ble_proc.poll() is None:
            return  # ya está corriendo

        log.info("Iniciando servidor BLE (ble_server.ts)…")
        env = os.environ.copy()
        env["NOBLE_MULTI_ROLE"] = "1"   # permite operar junto a bluetoothd

        try:
            _ble_proc = subprocess.Popen(
                [_BUN, "run", str(_BLE_SERVER_TS)],
                cwd=str(_THERMOPRINT_REF),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,  # crea nuevo grupo de procesos → killpg funciona
            )
            # Hilo que relanza la salida del servidor BLE al log de Python
            def _relay():
                for line in _ble_proc.stdout:
                    line = line.rstrip()
                    if line:
                        log.info("[BLE] %s", line)
            threading.Thread(target=_relay, daemon=True, name="ble-relay").start()

            # Esperar a que el servidor HTTP esté listo (máx 15 s)
            _wait_for_ble_server(timeout=15)
        except Exception as e:
            log.error("No se pudo iniciar ble_server.ts: %s", e)

def _wait_for_ble_server(timeout: float = 15) -> bool:
    """Espera hasta que el servidor responda en /ping."""
    import urllib.request, urllib.error
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BLE_SERVER_URL}/ping", timeout=1) as r:
                if r.status == 200:
                    log.info("Servidor BLE listo en %s", BLE_SERVER_URL)
                    return True
        except Exception:
            time.sleep(0.5)
    log.warning("Servidor BLE no respondió en %ss", timeout)
    return False

def _stop_ble_server() -> None:
    """Detiene el servidor BLE y todos sus procesos hijos."""
    global _ble_proc, _shuttingDown
    _shuttingDown = True
    import urllib.request
    # 1. Pedir shutdown limpio al servidor HTTP
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"{BLE_SERVER_URL}/shutdown", method="POST"),
            timeout=3,
        )
        time.sleep(0.8)
    except Exception:
        pass
    # 2. Matar el grupo de procesos completo (bun + hijos noble/bluetooth)
    with _ble_lock:
        proc = _ble_proc
        _ble_proc = None
    if proc and proc.poll() is None:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(0.5)
            if proc.poll() is None:
                os.killpg(pgid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
    log.info("Servidor BLE detenido.")

# Registrar shutdown al salir del proceso Python
import atexit
atexit.register(_stop_ble_server)

# ── Watchdog: reinicia ble_server si muere ────────────────────────────────
def _watchdog() -> None:
    """Hilo daemon que vigila el proceso bun y lo reinicia si cae."""
    global _ble_proc, _shuttingDown
    while not _shuttingDown:
        time.sleep(5)
        if _shuttingDown:
            break
        with _ble_lock:
            proc_dead = _ble_proc is None or _ble_proc.poll() is not None
        if proc_dead:
            log.warning("Servidor BLE caído — reiniciando…")
            _start_ble_server()

_watchdog_thread = threading.Thread(target=_watchdog, daemon=True, name="ble-watchdog")
_watchdog_thread.start()

# Arrancar servidor BLE al importar el módulo
_start_ble_server()

# ── Llamadas HTTP al servidor BLE ──────────────────────────────────────────
def _ble_get(path: str, timeout: float = 10) -> dict:
    import urllib.request, urllib.error
    try:
        with urllib.request.urlopen(f"{BLE_SERVER_URL}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())
    except Exception as e:
        return {"connected": False, "error": str(e)}

def _ble_post(path: str, data: dict, timeout: float = 45) -> dict:
    import urllib.request, urllib.error
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{BLE_SERVER_URL}{path}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _wait_ble_connected(timeout: float = 20.0) -> bool:
    """Espera a que el puente BLE tenga una conexión activa con la impresora."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _ble_get("/ping", timeout=2).get("connected"):
            return True
        time.sleep(0.4)
    return False

# ── Preparar imagen ─────────────────────────────────────────────────────────
def prepare_image(
    img: Image.Image,
    label_w_mm: int = DEFAULT_LABEL_W_MM,
    label_h_mm: int = DEFAULT_LABEL_H_MM,
) -> Image.Image:
    """Redimensiona la imagen al tamaño exacto de la etiqueta (sin márgenes extra).
    El canvas resultante es label_w_px × label_h_px.
    Tras rotar 90° en ble_server queda label_h_px × label_w_px (cabezal × avance).
    """
    label_w_px = label_w_mm * DOTS_PER_MM   # eje avance (largo de la etiqueta)
    label_h_px = label_h_mm * DOTS_PER_MM   # eje cabezal (ancho de la etiqueta)

    img = img.convert("RGB")
    # Redimensionar preservando proporción, ajustando al tamaño de la etiqueta
    img.thumbnail((label_w_px, label_h_px), Image.LANCZOS)

    # Canvas exactamente del tamaño de la etiqueta — sin relleno lateral
    canvas = Image.new("RGB", (label_w_px, label_h_px), (255, 255, 255))
    x_off = (label_w_px - img.width)  // 2
    y_off = (label_h_px - img.height) // 2
    canvas.paste(img, (x_off, y_off))
    return canvas

def image_to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ── API pública ─────────────────────────────────────────────────────────────
def print_image(
    img: Image.Image,
    density: int = 3,
    paper_type: str = "gap",
    label_w_mm: int = DEFAULT_LABEL_W_MM,
    label_h_mm: int = DEFAULT_LABEL_H_MM,
    copies: int = 1,
    post_feed_dots: int = 0,
) -> dict:
    """
    Imprime una imagen PIL en la P15 vía ble_server.ts.

    Con papel de etiquetas (``paper_type="gap"``) el protocolo envía
    ``positionToGap`` y el sensor avanza solo hasta el espacio en blanco:
    no hay que agregar avance extra o se salta la etiqueta siguiente.
    """
    density = max(1, min(3, density))
    if not _wait_ble_connected():
        return {
            "success": False,
            "message": "Impresora no conectada por BLE (revisa encendido/alcance)",
            "copies": [],
        }
    prepared = prepare_image(img, label_w_mm, label_h_mm)

    results = []
    for copy_num in range(copies):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
            prepared.save(tmp_path, format="PNG")
        try:
            result = _ble_post("/print", {
                "imagePath":    tmp_path,
                "density":      density,
                "paper":        paper_type,
                "width":        HEAD_W_PX,
                "postFeedDots": post_feed_dots,
            }, timeout=45)
            results.append({"copy": copy_num + 1, **result})
            log.info("Copia %d/%d: %s", copy_num + 1, copies,
                     "OK" if result.get("ok") else result.get("error", "ERROR"))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    all_ok = all(r.get("ok") for r in results)
    msg = "; ".join(r.get("message") or r.get("error", "") for r in results)
    return {"success": all_ok, "message": msg or ("✔ Impreso" if all_ok else "Error"), "copies": results}


def print_text_label(
    text: str,
    font_size: int = 40,
    density: int = 3,
    label_w_mm: int = DEFAULT_LABEL_W_MM,
    label_h_mm: int = DEFAULT_LABEL_H_MM,
) -> dict:
    try:
        from PIL import ImageDraw, ImageFont
    except ImportError:
        return {"success": False, "message": "Pillow no disponible"}

    w = label_w_mm * DOTS_PER_MM
    h = label_h_mm * DOTS_PER_MM
    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (OSError, IOError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((w - tw) // 2, (h - th) // 2), text, fill=(0, 0, 0), font=font)
    return print_image(img, density=density, label_w_mm=label_w_mm, label_h_mm=label_h_mm)


def get_status_simple() -> dict:
    """Estado de la impresora vía servidor BLE persistente."""
    result = _ble_get("/status", timeout=10)
    if result.get("connected"):
        return {
            "connected": True,
            "name":     result.get("name", ""),
            "id":       result.get("id", ""),
            "rssi":     result.get("rssi"),
            "status":   result.get("status", "ready"),
            "battery":  result.get("battery"),
        }
    return {"connected": False, "error": result.get("error", "Sin conexión")}


def discover_printers(timeout_ms: int = 5000) -> list[dict]:
    """
    Devuelve la impresora actualmente conectada al servidor BLE.
    No hace un scan adicional para no interrumpir la conexión existente.
    """
    status = get_status_simple()
    if status.get("connected"):
        return [{
            "name":  status.get("name", "P15"),
            "id":    status.get("id", ""),
            "rssi":  status.get("rssi", -100),
            "model": "p15",
        }]
    return []
