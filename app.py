"""
app.py — Servidor Flask para el diseñador de etiquetas P15
===========================================================
Ejecutar directamente:
    python app.py

O ejecutar con bandeja del sistema:
    python tray_app.py
"""

import base64
import io
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from PIL import Image

import printer as _printer_mod

# ── Config ───────────────────────────────────────────────────────────────────
# APP_DIR contiene los recursos empaquetados (templates/static). En el AppImage
# es de solo lectura; los datos escribibles van a DATA_DIR.
BASE_DIR = Path(os.environ.get("P15_APP_DIR") or Path(__file__).resolve().parent)


def _default_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
        return Path(base) / "P15-Designer"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/P15-Designer"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "p15-designer"


DATA_DIR = Path(os.environ.get("P15_DATA_DIR") or _default_data_dir())
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR    = BASE_DIR / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder=str(TEMPLATES_DIR),
    static_folder=str(STATIC_DIR),
)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

# ── Rutas principales ────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(str(TEMPLATES_DIR), "index.html")

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(str(STATIC_DIR), filename)


# ── API: info de etiqueta (para el canvas) ───────────────────────────────────
@app.route("/api/label_info", methods=["GET"])
def api_label_info():
    return jsonify({
        "ok": True,
        "width_mm":        _printer_mod.DEFAULT_LABEL_W_MM,
        "height_mm":       _printer_mod.DEFAULT_LABEL_H_MM,
        "head_width_px":   _printer_mod.HEAD_W_PX,
        "content_width_px": _printer_mod.DEFAULT_LABEL_W_MM * _printer_mod.DOTS_PER_MM,
        "content_height_px": _printer_mod.DEFAULT_LABEL_H_MM * _printer_mod.DOTS_PER_MM,
        "dpi": 203,
    })


# ── API: descubrir impresoras ────────────────────────────────────────────────
@app.route("/api/discover", methods=["GET"])
def api_discover():
    timeout = int(request.args.get("timeout", 5000))
    try:
        printers = _printer_mod.discover_printers(timeout_ms=timeout)
        return jsonify({"ok": True, "printers": printers})
    except Exception as e:
        log.exception("Error al descubrir")
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: connect/disconnect (el CLI no mantiene conexión; son no-ops) ─────────
@app.route("/api/connect", methods=["POST"])
def api_connect():
    """El CLI descubre la impresora en cada operación. Aquí solo confirmamos."""
    body = request.get_json(force=True) or {}
    name = body.get("address") or body.get("name") or "impresora"
    return jsonify({"ok": True, "message": f"Configurada: {name}", "address": name})


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    return jsonify({"ok": True, "message": "Desconectada"})


# ── API: estado de la impresora ──────────────────────────────────────────────
@app.route("/api/status", methods=["GET"])
def api_status():
    """Estado de la impresora (sin imprimir)."""
    try:
        status = _printer_mod.get_status_simple()
        return jsonify({"ok": True, **status})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: configurar permisos Bluetooth (AppImage) ───────────────────────────
@app.route("/api/ble/setup", methods=["POST"])
def api_ble_setup():
    """Copia bun a un directorio escribible y le aplica cap_net_raw."""
    try:
        result = _printer_mod.setup_ble_permissions()
        return jsonify(result), (200 if result.get("ok") else 500)
    except Exception as e:
        log.exception("Error configurando permisos BLE")
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: impresión ────────────────────────────────────────────────────────────
@app.route("/api/print", methods=["POST"])
def api_print():
    """
    Recibe imagen PNG en base64 (del canvas) y la envía a la impresora.
    Body JSON:
      {
        "image":   "<base64 PNG>",
        "density": 3,          // 1-3
        "copies":  1,
        "paper":   "gap",      // "gap" | "continuous"
        "width_mm":  38,       // opcional, ancho de etiqueta
        "height_mm": 12        // opcional, alto de etiqueta
      }
    """
    body    = request.get_json(force=True)
    img_b64 = body.get("image", "")
    density = int(body.get("density", 3))
    copies  = int(body.get("copies", 1))
    paper   = body.get("paper", "gap")
    w_mm    = int(body.get("width_mm",  _printer_mod.DEFAULT_LABEL_W_MM))
    h_mm    = int(body.get("height_mm", _printer_mod.DEFAULT_LABEL_H_MM))
    # Con etiquetas (gap) el sensor posiciona la siguiente: sin avance extra.
    # Con papel continuo se deja un pequeño avance para poder cortar.
    post_feed_dots = int(body.get('post_feed_dots', 0 if paper == 'gap' else 64))

    if not img_b64:
        return jsonify({"ok": False, "error": "Sin imagen"}), 400

    # Decodificar base64
    if "," in img_b64:
        img_b64 = img_b64.split(",", 1)[1]
    try:
        img = Image.open(io.BytesIO(base64.b64decode(img_b64)))
    except Exception as e:
        return jsonify({"ok": False, "error": f"Imagen inválida: {e}"}), 400

    try:
        result = _printer_mod.print_image(
            img,
            density=density,
            paper_type=paper,
            label_w_mm=w_mm,
            label_h_mm=h_mm,
            copies=copies,
            post_feed_dots=post_feed_dots,
        )
        if result["success"]:
            return jsonify({"ok": True, "message": result["message"]})
        else:
            return jsonify({"ok": False, "error": result["message"]}), 500
    except Exception as e:
        log.exception("Error al imprimir")
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: previsualización ────────────────────────────────────────────────────
@app.route("/api/preview", methods=["POST"])
def api_preview():
    """
    Recibe imagen base64 y devuelve la versión procesada tal como la
    vería la impresora (grayscale → dither → 1bpp → RGB para visualizar).
    """
    body    = request.get_json(force=True)
    img_b64 = body.get("image", "")
    w_mm    = int(body.get("width_mm",  _printer_mod.DEFAULT_LABEL_W_MM))
    h_mm    = int(body.get("height_mm", _printer_mod.DEFAULT_LABEL_H_MM))

    if "," in img_b64:
        img_b64 = img_b64.split(",", 1)[1]

    try:
        img = Image.open(io.BytesIO(base64.b64decode(img_b64)))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    # Preparar igual que para imprimir
    prepared = _printer_mod.prepare_image(img, w_mm, h_mm)

    # Simular pipeline de impresión: gris → umbral → RGB
    preview = prepared.convert("L").point(lambda p: 0 if p < 128 else 255).convert("RGB")

    buf = io.BytesIO()
    preview.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return jsonify({
        "ok": True,
        "preview": f"data:image/png;base64,{b64}",
        "width":  preview.width,
        "height": preview.height,
    })


# ── API: subir imagen ────────────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Sin archivo"}), 400
    try:
        img = Image.open(request.files["file"]).convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return jsonify({
            "ok": True,
            "image": f"data:image/png;base64,{b64}",
            "width": img.width,
            "height": img.height,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: detener servidor ────────────────────────────────────────────────────
@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Detiene el servidor limpiamente desde el navegador."""
    log.info("Detener solicitado desde la interfaz web")
    def _shutdown():
        _printer_mod._stop_ble_server()
        os._exit(0)
    threading.Timer(0.3, _shutdown).start()
    return jsonify({"ok": True, "message": "Servidor detenido"})


# ── API: imprimir texto directo ──────────────────────────────────────────────
@app.route("/api/print_text", methods=["POST"])
def api_print_text():
    body      = request.get_json(force=True)
    text      = body.get("text", "").strip()
    density   = int(body.get("density", 3))
    font_size = int(body.get("font_size", 40))

    if not text:
        return jsonify({"ok": False, "error": "Texto vacío"}), 400
    try:
        result = _printer_mod.print_text_label(text, font_size=font_size, density=density)
        return jsonify({"ok": result["success"], "message": result["message"]})
    except Exception as e:
        log.exception("Error al imprimir texto")
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Main ──────────────────────────────────────────────────────────────────────
def run_server(port: int = 5000, open_browser: bool = True):
    import signal

    # Asegurar que Ctrl+C siempre mata el proceso y bun, incluso desde un hilo
    def _handle_sigint(sig, frame):
        log.info("Servidor detenido (señal %s)", sig)
        _printer_mod._stop_ble_server()
        os._exit(0)

    # signal.signal() solo funciona en el hilo principal (tray_app usa un hilo)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT,  _handle_sigint)
        signal.signal(signal.SIGTERM, _handle_sigint)

    url = f"http://localhost:{port}"
    log.info("Servidor iniciado en %s", url)
    log.info("Presiona Ctrl+C para detener")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    run_server()
