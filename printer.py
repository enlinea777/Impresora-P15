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
_APP_DIR         = Path(os.environ.get("P15_APP_DIR") or Path(__file__).resolve().parent)
_THERMOPRINT_REF = Path(os.environ.get("P15_THERMOPRINT_DIR") or _APP_DIR / "thermoprint_ref")
_BLE_SERVER_TS   = _THERMOPRINT_REF / "packages" / "cli" / "src" / "ble_server.ts"
_THERMOPRINT_CLI = _THERMOPRINT_REF / "packages" / "cli" / "src" / "index.ts"

# Directorio de datos del usuario (escribible). En el AppImage el resto es
# de solo lectura, así que aquí van logs, ajustes y la copia de bun con permisos.
def _default_data_dir() -> Path:
    if env := os.environ.get("P15_DATA_DIR"):
        return Path(env)
    if _OS == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
        return Path(base) / "P15-Designer"
    if _OS == "Darwin":
        return Path.home() / "Library/Application Support/P15-Designer"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "p15-designer"

DATA_DIR  = _default_data_dir()
_LOCAL_BUN = DATA_DIR / "bin" / "bun"
_BUNDLED_BUN = _THERMOPRINT_REF / "bun"   # incluido en el AppImage

BLE_SERVER_PORT  = int(os.environ.get("P15_BLE_PORT", "7315"))
BLE_SERVER_URL   = f"http://localhost:{BLE_SERVER_PORT}"

# ── Buscar bun ─────────────────────────────────────────────────────────────
def _bun_candidates() -> list[Path]:
    """Rutas de bun por orden de prioridad (la copia local puede tener caps)."""
    candidates: list[Path] = []
    for value in (os.environ.get("P15_BUN"), os.environ.get("BUN_BIN")):
        if value:
            candidates.append(Path(value))
    candidates.append(_LOCAL_BUN)
    candidates.append(_BUNDLED_BUN)

    if found := shutil.which("bun"):
        candidates.append(Path(found))

    if _OS == "Windows":
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        candidates += [
            Path(appdata) / "bun" / "bin" / "bun.exe",
            Path(localappdata) / "bun" / "bin" / "bun.exe",
        ]
    elif _OS == "Darwin":
        candidates += [Path.home() / ".bun/bin/bun", Path("/usr/local/bin/bun"), Path("/opt/homebrew/bin/bun")]
    else:
        candidates += [Path.home() / ".bun/bin/bun", Path("/usr/local/bin/bun")]
    return candidates


def _find_bun() -> str:
    for c in _bun_candidates():
        if c.exists():
            return str(c)
    return "bun"


_BUN = _find_bun()


# ── Permisos Bluetooth (Linux) ────────────────────────────────────────────
def _caps_ok(path: str) -> bool:
    """True si el binario puede abrir sockets HCI (root o con cap_net_raw)."""
    if _OS != "Linux":
        return True
    if os.geteuid() == 0:
        return True
    try:
        out = subprocess.run(
            ["getcap", str(path)],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # no se puede comprobar → no bloquear
    return "cap_net_raw" in out


def ble_permissions_ok() -> bool:
    """Indica si el bun activo tiene permisos para usar Bluetooth."""
    return _caps_ok(_BUN)


def _switch_bun(path: str) -> None:
    """Cambia el binario de bun y reinicia el servidor BLE en segundo plano."""
    global _BUN
    log.info("Usando bun: %s", path)
    _BUN = str(path)
    threading.Thread(target=_restart_ble_server, daemon=True, name="ble-restart").start()


def _restart_ble_server() -> None:
    _stop_ble_server()
    globals()["_shuttingDown"] = False
    _start_ble_server()


def setup_ble_permissions() -> dict:
    """
    Copia bun a un directorio escribible y le aplica cap_net_raw/cap_net_admin.

    En el AppImage el binario vive en una imagen de solo lectura y no puede
    conservar file capabilities, por eso se copia a ~/.local/share.
    """
    if _OS != "Linux":
        return {"ok": True, "message": "No hace falta configurar permisos en este sistema."}

    src = Path(_BUNDLED_BUN)
    if not src.exists():
        src = Path(_BUN)
    if not src.exists():
        return {"ok": False, "error": "No se encontró el binario de bun."}

    dst = _LOCAL_BUN
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            log.info("Copiando bun a %s…", dst)
            shutil.copy2(src, dst)
        dst.chmod(0o755)
    except Exception as e:
        return {"ok": False, "error": f"No se pudo copiar bun: {e}"}

    setcap_args = ["setcap", "cap_net_raw,cap_net_admin+eip", str(dst)]
    attempts: list[list[str]] = []
    if shutil.which("pkexec"):
        attempts.append(["pkexec", *setcap_args])      # diálogo gráfico
    if shutil.which("sudo"):
        attempts.append(["sudo", "-n", *setcap_args])  # sin preguntar
    if os.geteuid() == 0:
        attempts.insert(0, setcap_args)

    errors = []
    for cmd in attempts:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except Exception as e:
            errors.append(f"{cmd[0]}: {e}")
            continue
        if proc.returncode == 0 and _caps_ok(str(dst)):
            _switch_bun(str(dst))
            return {
                "ok": True,
                "message": "Permisos Bluetooth configurados. Reconectando…",
                "bun": str(dst),
            }
        errors.append((proc.stderr or proc.stdout or f"{cmd[0]}: código {proc.returncode}").strip())

    manual = f"sudo setcap cap_net_raw,cap_net_admin+eip {dst}"
    log.warning("No se pudieron aplicar permisos BLE: %s", "; ".join(errors))
    return {
        "ok": False,
        "error": "No se pudieron aplicar los permisos automáticamente.",
        "manual": manual,
        "details": errors,
    }

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

        if not Path(_BUN).exists():
            log.error("No se encontró bun (%s). El Bluetooth no estará disponible.", _BUN)
            return
        if not _BLE_SERVER_TS.exists():
            log.error("No se encontró %s. El Bluetooth no estará disponible.", _BLE_SERVER_TS)
            return
        if _OS == "Linux" and not _caps_ok(_BUN):
            log.warning(
                "bun no tiene permisos Bluetooth (cap_net_raw). "
                "Usa «Permitir Bluetooth» en la interfaz o ejecuta el AppImage con "
                "--setup-bluetooth."
            )

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
    perms_ok = ble_permissions_ok()
    if result.get("connected"):
        return {
            "connected": True,
            "name":     result.get("name", ""),
            "id":       result.get("id", ""),
            "rssi":     result.get("rssi"),
            "status":   result.get("status", "ready"),
            "battery":  result.get("battery"),
            "ble_permissions_ok": perms_ok,
        }

    status = {
        "connected": False,
        "error": result.get("error", "Sin conexión"),
        "ble_permissions_ok": perms_ok,
        "setup_available": Path(_BUNDLED_BUN).exists() or Path(_BUN).exists(),
    }
    if not perms_ok:
        status["error"] = (
            "Bluetooth sin permisos. Pulsa «Permitir Bluetooth» o ejecuta "
            "el AppImage con --setup-bluetooth."
        )
        status["error_code"] = "ble_permissions"
    return status


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
