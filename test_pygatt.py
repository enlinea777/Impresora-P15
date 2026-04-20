#!/usr/bin/env python3
"""
test_pygatt.py — P15 Niimbot BLE — Protocolo L11 Binary (correcto)
====================================================================
Basado en ingeniería inversa del APK y el proyecto thermoprint:
  https://github.com/tomLadder/thermoprint

Características BLE de la P15:
  Service   : 0000ff00-...
  TX (write) : 0000ff02-...  ← host → impresora
  RX (notify): 0000ff01-...  ← impresora → host (respuestas)
  CX (notify): 0000ff03-...  ← créditos de flujo y MTU

Uso:
    python test_pygatt.py                     # imprime patrón sólido
    python test_pygatt.py --pattern border    # solo borde
    python test_pygatt.py --pattern stripes   # rayas horizontales
    python test_pygatt.py --density 1-5       # densidad (defecto: 2)
    python test_pygatt.py --battery           # nivel batería
    python test_pygatt.py --status            # estado impresora
    python test_pygatt.py --model             # modelo
    python test_pygatt.py --self-check        # auto-diagnóstico
    python test_pygatt.py --no-print          # sin imprimir (solo info)
    python test_pygatt.py --mac AA:BB:CC:...  # otra MAC
"""

import argparse
import threading
import time

# ── Dependencias ──────────────────────────────────────────────────────────────
def _ensure(mod, pip_name):
    try:
        return __import__(mod)
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name],
                              stdout=subprocess.DEVNULL)
        return __import__(mod)

pygatt = _ensure("pygatt", "pygatt")

# ── Configuración ─────────────────────────────────────────────────────────────
MAC = "06:0E:4B:31:EE:A6"

CHAR_TX = "0000ff02-0000-1000-8000-00805f9b34fb"   # Write (host → impresora)
CHAR_RX = "0000ff01-0000-1000-8000-00805f9b34fb"   # Notify (impresora → host)
CHAR_CX = "0000ff03-0000-1000-8000-00805f9b34fb"   # Credits / MTU

PACKET_SIZE  = 95       # bytes máx por write BLE (P15 hardcoded)
PACKET_DELAY = 0.030    # 30 ms entre paquetes (perfil P15)
INIT_CREDITS = 0        # créditos vienen del CX ([0x01, N]) — no pre-asignar

# Dimensiones del cabezal y etiqueta @ 8 dots/mm (203 DPI)
#   Cabezal físico P15 : 48mm → 384 px → 48 bytes/fila  (ancho real del head)
#   Etiqueta por defecto: 40×12 mm
#     widthMm=38  → 304 px de contenido, centrado dentro de los 384 px del cabezal
#     heightMm=12 → 96 px de alto (dirección de avance del papel)
HEAD_W_MM     = 48                                # ancho físico del cabezal
LABEL_W_MM    = 38    # 38mm = 304px (ajustado: 40mm=320px se pasaba 1-2px del borde)
LABEL_H_MM    = 12
DOTS_PER_MM   = 8
BYTES_PER_ROW = HEAD_W_MM * DOTS_PER_MM // 8    # 48 — siempre el cabezal completo
LABEL_H_PX    = LABEL_H_MM * DOTS_PER_MM         # 96

# ── Comandos L11 ──────────────────────────────────────────────────────────────

def cmd_wakeup():
    """15 bytes cero para despertar la impresora"""
    return bytes(15)

def cmd_enable():
    """Activa el motor de impresión: 10 FF F1 02"""
    return bytes([0x10, 0xFF, 0xF1, 0x02])

def cmd_stop():
    """Termina la sesión de impresión: 10 FF F1 45"""
    return bytes([0x10, 0xFF, 0xF1, 0x45])

def cmd_set_density(density):
    """Ajusta densidad: 1F 70 02 DD"""
    return bytes([0x1F, 0x70, 0x02, density & 0xFF])

def cmd_feed_dots(dots):
    """Avanzar N dots: 1B 4A NN"""
    return bytes([0x1B, 0x4A, dots & 0xFF])

def cmd_position_to_gap():
    """Avanzar hasta el próximo gap de etiqueta: 1D 0C"""
    return bytes([0x1D, 0x0C])

def cmd_get_battery():
    """Consultar batería: 10 FF 50 F1"""
    return bytes([0x10, 0xFF, 0x50, 0xF1])

def cmd_get_status():
    """Estado de impresora: 10 FF 40"""
    return bytes([0x10, 0xFF, 0x40])

def cmd_get_model():
    """Modelo de impresora: 10 FF 20 F0"""
    return bytes([0x10, 0xFF, 0x20, 0xF0])

def cmd_get_firmware():
    """Versión firmware: 10 FF 20 F1"""
    return bytes([0x10, 0xFF, 0x20, 0xF1])

def cmd_get_serial():
    """Número de serie: 10 FF 20 F2"""
    return bytes([0x10, 0xFF, 0x20, 0xF2])

def cmd_self_check():
    """Impresión de auto-diagnóstico: 1F 40"""
    return bytes([0x1F, 0x40])

def cmd_get_detailed_status():
    """Estado detallado: 1F 20 00"""
    return bytes([0x1F, 0x20, 0x00])

def cmd_print_bitmap(pixels_1bpp, bytes_per_row, height, quality=0):
    """
    Comando raster bitmap: 1D 76 30 QQ WL WH HL HH + datos
    quality   : 0-3 (normalmente 0)
    pixels_1bpp: bitmap 1bpp empaquetado MSB-first, 1=negro, 0=blanco
    """
    header = bytes([
        0x1D, 0x76, 0x30, quality & 0x03,
        bytes_per_row & 0xFF, (bytes_per_row >> 8) & 0xFF,
        height & 0xFF,        (height >> 8) & 0xFF,
    ])
    return header + bytes(pixels_1bpp)

# ── Generadores de imágenes de prueba ─────────────────────────────────────────

def _label_content_range(head_bpr, label_w_mm):
    """Devuelve (start_byte, end_byte) del contenido de etiqueta centrado en el cabezal."""
    content_bytes = label_w_mm * DOTS_PER_MM // 8
    pad = (head_bpr - content_bytes) // 2
    return pad, pad + content_bytes

def make_solid_bitmap(bytes_per_row=BYTES_PER_ROW, height=LABEL_H_PX):
    """Etiqueta completamente negra (contenido centrado en el cabezal)"""
    data = bytearray(bytes_per_row * height)
    start, end = _label_content_range(bytes_per_row, LABEL_W_MM)
    for row in range(height):
        for col in range(start, end):
            data[row * bytes_per_row + col] = 0xFF
    return bytes(data)

def make_border_bitmap(bytes_per_row=BYTES_PER_ROW, height=LABEL_H_PX):
    """Solo borde exterior del área de etiqueta (1 px)"""
    data = bytearray(bytes_per_row * height)
    start, end = _label_content_range(bytes_per_row, LABEL_W_MM)
    for row in range(height):
        if row == 0 or row == height - 1:
            # línea horizontal completa
            for col in range(start, end):
                data[row * bytes_per_row + col] = 0xFF
        else:
            # bordes verticales izquierdo y derecho
            data[row * bytes_per_row + start] = 0x80         # pixel izquierdo
            data[row * bytes_per_row + end - 1] = 0x01       # pixel derecho
    return bytes(data)

def make_stripes_bitmap(bytes_per_row=BYTES_PER_ROW, height=LABEL_H_PX):
    """Rayas horizontales alternas cada 8 filas"""
    data = bytearray(bytes_per_row * height)
    start, end = _label_content_range(bytes_per_row, LABEL_W_MM)
    for row in range(height):
        val = 0xFF if (row // 8) % 2 == 0 else 0x00
        for col in range(start, end):
            data[row * bytes_per_row + col] = val
    return bytes(data)

def make_checkerboard_bitmap(bytes_per_row=BYTES_PER_ROW, height=LABEL_H_PX):
    """Tablero de ajedrez 8×8 px"""
    data = bytearray(bytes_per_row * height)
    start, end = _label_content_range(bytes_per_row, LABEL_W_MM)
    for row in range(height):
        for col in range(start, end):
            val = 0xF0 if ((row // 8) + (col - start)) % 2 == 0 else 0x0F
            data[row * bytes_per_row + col] = val
    return bytes(data)

# ── Clase principal ───────────────────────────────────────────────────────────

class P15Printer:
    STATUS_CODES = {
        0x01: "sin_papel",
        0x02: "tapa_abierta",
        0x03: "sobrecalentamiento",
        0x04: "batería_baja",
        0x05: "tapa_cerrada",
    }

    def __init__(self, mac=MAC):
        self.mac = mac
        self.adapter = None
        self.device  = None
        # Control de flujo por créditos
        self._credits       = INIT_CREDITS
        self._credits_lock  = threading.Lock()
        self._credits_event = threading.Event()
        self._credits_event.set()   # créditos iniciales disponibles
        # Respuestas
        self.rx_log       = []
        self.print_success = False

    # ── Callbacks BLE ────────────────────────────────────────────────────────

    def _cx_callback(self, handle, value):
        """Notificaciones del canal CX: créditos y MTU"""
        if not value:
            return
        if value[0] == 0x01 and len(value) >= 2:
            n = value[1]
            with self._credits_lock:
                self._credits += n
            print(f"  [CX] +{n} créditos  (total: {self._credits})")
            self._credits_event.set()
        elif value[0] == 0x02 and len(value) >= 3:
            mtu = value[1] | (value[2] << 8)
            print(f"  [CX] MTU anunciado: {mtu} bytes")
        else:
            print(f"  [CX] Desconocido: {bytes(value).hex()}")

    def _rx_callback(self, handle, value):
        """Notificaciones del canal RX: respuestas de la impresora"""
        if not value:
            return
        raw = bytes(value)
        self.rx_log.append(raw)
        first = raw[0]

        if first in (0xAA, 0x4F, 0x4B):
            print(f"  [RX] ✅ ÉXITO: {raw.hex()}")
            self.print_success = True
        elif first == 0xFF and len(raw) >= 2:
            desc = self.STATUS_CODES.get(raw[1], f"desconocido_{raw[1]:02x}")
            print(f"  [RX] Estado: {desc}  ({raw.hex()})")
        else:
            try:
                txt = raw.decode("ascii")
                print(f"  [RX] Texto: {txt!r}  ({raw.hex()})")
            except Exception:
                print(f"  [RX] {raw.hex()}")

    # ── Conexión ──────────────────────────────────────────────────────────────

    def connect(self):
        self.adapter = pygatt.GATTToolBackend()
        self.adapter.start()
        print(f"Conectando a {self.mac} …")
        self.device = self.adapter.connect(
            self.mac,
            timeout=15,
            address_type=pygatt.BLEAddressType.public,
        )
        print("✓ Conectado")
        self.device.subscribe(CHAR_RX, callback=self._rx_callback)
        self.device.subscribe(CHAR_CX, callback=self._cx_callback)
        print("✓ Suscrito a RX y CX")
        time.sleep(0.5)   # dar tiempo a notificaciones MTU/créditos iniciales

    def disconnect(self):
        if self.adapter:
            try:
                self.adapter.stop()
            except Exception:
                pass

    # ── Escritura con control de créditos ─────────────────────────────────────

    def _wait_credit(self, timeout=5.0):
        """Bloquea hasta tener ≥1 crédito y lo consume."""
        deadline = time.time() + timeout
        while True:
            with self._credits_lock:
                if self._credits > 0:
                    self._credits -= 1
                    return True
            remaining = deadline - time.time()
            if remaining <= 0:
                print("  [WARN] Timeout esperando crédito — enviando igualmente")
                return False
            self._credits_event.clear()
            self._credits_event.wait(timeout=min(0.2, remaining))

    def write(self, data, use_credits=True):
        """Envía un comando simple (sin chunking)."""
        if use_credits:
            self._wait_credit()
        self.device.char_write(CHAR_TX, bytearray(data), wait_for_response=False)
        time.sleep(PACKET_DELAY)

    def write_bulk(self, data):
        """Envía datos grandes en fragmentos de PACKET_SIZE con créditos."""
        total     = len(data)
        offset    = 0
        chunk_idx = 0
        while offset < total:
            chunk = data[offset: offset + PACKET_SIZE]
            self._wait_credit()
            self.device.char_write(CHAR_TX, bytearray(chunk), wait_for_response=False)
            chunk_idx += 1
            offset += PACKET_SIZE
            time.sleep(PACKET_DELAY)
            if chunk_idx % 5 == 0:
                print(f"  … chunk {chunk_idx}  ({min(offset, total)}/{total} bytes)")
        print(f"  … {chunk_idx} chunks enviados ({total} bytes)")

    # ── Secuencia de impresión ────────────────────────────────────────────────

    def print_image(self, pixels_1bpp, bytes_per_row=BYTES_PER_ROW,
                    height=LABEL_H_PX, density=2):
        """
        Secuencia L11:
          setDensity → wakeup → enable → printBitmap → positionToGap → stop
        """
        print(f"\nSecuencia de impresión L11:")
        print(f"  Cabezal: {bytes_per_row*8}px ({bytes_per_row} bytes/fila) × {height} filas")
        print(f"  Densidad: {density}")

        print("→ set_density")
        self.write(cmd_set_density(density))

        print("→ wakeup  (15 bytes cero)")
        self.write(cmd_wakeup())

        print("→ enable")
        self.write(cmd_enable())

        print("→ print_bitmap  (bulk, chunked)")
        bitmap_cmd = cmd_print_bitmap(pixels_1bpp, bytes_per_row, height)
        n_chunks = (len(bitmap_cmd) + PACKET_SIZE - 1) // PACKET_SIZE
        print(f"  Tamaño del comando: {len(bitmap_cmd)} bytes → {n_chunks} chunks de ≤{PACKET_SIZE}")
        self.write_bulk(bitmap_cmd)

        print("→ position_to_gap  (paper_type=gap, default P15)")
        self.write(cmd_position_to_gap())

        print("→ stop")
        self.write(cmd_stop())

        print("\nEsperando respuesta de la impresora …")
        for _ in range(40):   # hasta 4 segundos
            time.sleep(0.1)
            if self.print_success:
                print("✅ Impresión confirmada por la impresora")
                return True
        print("⚠  Sin confirmación de éxito (puede que haya imprimido igual)")
        return False

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Test P15 Niimbot BLE — Protocolo L11",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mac",       default=MAC,
                        help="MAC de la impresora")
    parser.add_argument("--density",   type=int, default=2, choices=range(1, 6),
                        metavar="1-5", help="Densidad de impresión (defecto: 2)")
    parser.add_argument("--pattern",   default="solid",
                        choices=["solid", "border", "stripes", "checker"],
                        help="Patrón de prueba (defecto: solid)")
    parser.add_argument("--width-mm",  type=int, default=LABEL_W_MM,
                        help=f"Ancho etiqueta en mm (defecto: {LABEL_W_MM})")
    parser.add_argument("--height-mm", type=int, default=LABEL_H_MM,
                        help=f"Alto etiqueta en mm (defecto: {LABEL_H_MM})")
    # Consultas de información
    parser.add_argument("--battery",   action="store_true", help="Leer batería")
    parser.add_argument("--status",    action="store_true", help="Leer estado")
    parser.add_argument("--model",     action="store_true", help="Leer modelo")
    parser.add_argument("--firmware",  action="store_true", help="Leer firmware")
    parser.add_argument("--serial",    action="store_true", help="Leer serie")
    parser.add_argument("--self-check",action="store_true", dest="self_check",
                        help="Página de auto-diagnóstico")
    parser.add_argument("--no-print",  action="store_true", dest="no_print",
                        help="No imprimir (solo consultas)")
    args = parser.parse_args()

    # El cabezal siempre es 48mm (384px = 48 bytes/fila)
    # La etiqueta se centra dentro del cabezal
    bpr = HEAD_W_MM * DOTS_PER_MM // 8         # 48 bytes/fila (cabezal completo)
    hpx = args.height_mm * DOTS_PER_MM         # filas (dots)
    print(f"Cabezal: {HEAD_W_MM}mm ({bpr*8}px, {bpr} bytes/fila)  "
          f"Etiqueta: {args.width_mm}×{args.height_mm}mm ({args.width_mm*DOTS_PER_MM}×{hpx}px)")

    printer = P15Printer(args.mac)
    try:
        printer.connect()

        if args.battery:
            print("\n→ get_battery")
            printer.write(cmd_get_battery())
            time.sleep(1.5)

        if args.status:
            print("\n→ get_status")
            printer.write(cmd_get_status())
            time.sleep(1.5)

        if args.model:
            print("\n→ get_model")
            printer.write(cmd_get_model())
            time.sleep(1.5)

        if args.firmware:
            print("\n→ get_firmware")
            printer.write(cmd_get_firmware())
            time.sleep(1.5)

        if args.serial:
            print("\n→ get_serial")
            printer.write(cmd_get_serial())
            time.sleep(1.5)

        if args.self_check:
            print("\n→ self_check  (imprimirá diagnóstico interno)")
            printer.write(cmd_self_check())
            time.sleep(4.0)

        if not args.no_print:
            pat = args.pattern
            if pat == "solid":
                pixels = make_solid_bitmap(bpr, hpx)
            elif pat == "border":
                pixels = make_border_bitmap(bpr, hpx)
            elif pat == "stripes":
                pixels = make_stripes_bitmap(bpr, hpx)
            else:
                pixels = make_checkerboard_bitmap(bpr, hpx)

            print(f"\nPatrón seleccionado: {pat}")
            printer.print_image(pixels, bytes_per_row=bpr, height=hpx, density=args.density)

    except KeyboardInterrupt:
        print("\n[interrumpido por usuario]")
    except Exception as e:
        print(f"\n[ERROR] {e}")
        raise
    finally:
        printer.disconnect()
        print("\nDesconectado.")


if __name__ == "__main__":
    main()
