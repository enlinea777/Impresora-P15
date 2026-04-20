#!/usr/bin/env python3
"""
test_printer.py — Test automatizado completo para la impresora P15 Niimbot
=========================================================================
Ejecutar:  python test_printer.py
           python test_printer.py --target P15_EEA6_BLE
           python test_printer.py --target AA:BB:CC:DD:EE:FF
           python test_printer.py --scan-only
           python test_printer.py --info-only

El script:
  1. Verifica el adaptador Bluetooth (hciconfig / bluetoothctl)
  2. Escanea dispositivos BLE (varios intentos con distintos timeouts)
  3. Resuelve nombre → MAC si es necesario
  4. Conecta con reintentos y variantes de config
  5. Descubre servicios y características
  6. Suscribe notificaciones
  7. Solicita info del dispositivo (CMD_GET_INFO)
  8. Genera una imagen de prueba en memoria
  9. Envía la imagen a imprimir
  10. Informa resultado final con diagnóstico si algo falla

Todo queda registrado en  test_printer.log  (y en la consola).
"""

import argparse
import asyncio
import io
import logging
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

# ─── Verificar dependencias antes de importarlas ──────────────────────────
def _check_dep(module: str, pip_name: str):
    try:
        __import__(module)
    except ImportError:
        print(f"[!] Falta dependencia: {pip_name}. Instalando…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])

_check_dep("bleak", "bleak")
_check_dep("PIL",   "pillow")

from bleak import BleakClient, BleakScanner, BleakError
from bleak.backends.device import BLEDevice
from PIL import Image, ImageDraw, ImageFont

# ─── Logging ──────────────────────────────────────────────────────────────
LOG_FILE = Path(__file__).parent / "test_printer.log"
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("p15test")

# ─── Constantes de protocolo Niimbot ──────────────────────────────────────
# UUID detectado en el advertising de la P15 (e7810a71…) — Niimbot real
SERVICE_UUID = "e7810a71-73ae-499d-8c15-faa9aef0c3f2"  # detectado en scan
WRITE_UUID   = "0000ff02-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID  = "0000ff01-0000-1000-8000-00805f9b34fb"

# UUIDs alternativos — se prueban todos hasta encontrar el correcto
ALT_WRITE_UUIDS = [
    "0000ff02-0000-1000-8000-00805f9b34fb",
    "0000ff03-0000-1000-8000-00805f9b34fb",
    "49535343-8841-43f4-a8d4-ecbe34729bb3",
    "0000ae02-0000-1000-8000-00805f9b34fb",
    "e7810a71-73ae-499d-8c15-faa9aef0c3f2",  # por si el servicio=write
]
ALT_NOTIFY_UUIDS = [
    "0000ff01-0000-1000-8000-00805f9b34fb",
    "0000ff04-0000-1000-8000-00805f9b34fb",
    "49535343-1e4d-4bd9-ba61-23c647249616",
    "0000ae01-0000-1000-8000-00805f9b34fb",
]

CMD_GET_INFO       = 0x40
CMD_SET_LABEL_TYPE = 0x23
CMD_SET_LABEL_DENS = 0x21
CMD_START_PRINT    = 0x01
CMD_END_PRINT      = 0xF3
CMD_ALLOW_PRINT    = 0x1A
CMD_SET_PAGE_SIZE  = 0x03
CMD_PRINT_ROW      = 0x85
CMD_PRINT_EMPTY    = 0x84

DPI            = 203
LABEL_W_MM     = 12
LABEL_H_MM     = 40
PRINT_W_PX     = round(LABEL_W_MM * DPI / 25.4)   # 96 px
PRINT_H_PX     = round(LABEL_H_MM * DPI / 25.4)   # 320 px

PRINTER_KEYWORDS = ["p15","p18","niimbot","label","print","b21","b3s","d11","eea6"]
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

# ─── Colores ANSI ─────────────────────────────────────────────────────────
G  = "\033[92m"   # verde
Y  = "\033[93m"   # amarillo
R  = "\033[91m"   # rojo
B  = "\033[94m"   # azul
W  = "\033[97m"   # blanco brillante
DIM= "\033[2m"
RST= "\033[0m"

def ok(msg):  log.info(f"{G}✔ {msg}{RST}")
def warn(msg):log.warning(f"{Y}⚠ {msg}{RST}")
def err(msg): log.error(f"{R}✘ {msg}{RST}")
def hdr(msg): log.info(f"\n{W}{'─'*60}\n  {msg}\n{'─'*60}{RST}")

# ══════════════════════════════════════════════════════════════════════════
# PASO 1 — Diagnóstico del adaptador Bluetooth
# ══════════════════════════════════════════════════════════════════════════
def check_bluetooth_adapter() -> bool:
    hdr("PASO 1 — Diagnóstico del adaptador Bluetooth")
    ok_flag = True

    # hciconfig
    try:
        r = subprocess.run(["hciconfig"], capture_output=True, text=True, timeout=5)
        log.info("hciconfig:\n%s", r.stdout.strip() or "(sin salida)")
        if "hci0" not in r.stdout:
            warn("No se detectó hci0. ¿Está el adaptador BT activado?")
            ok_flag = False
        elif "UP RUNNING" not in r.stdout:
            warn("hci0 existe pero no está UP RUNNING. Intentando levantarlo…")
            subprocess.run(["sudo","hciconfig","hci0","up"], capture_output=True)
            time.sleep(1)
            r2 = subprocess.run(["hciconfig"], capture_output=True, text=True)
            if "UP RUNNING" in r2.stdout:
                ok("hci0 levantado correctamente")
            else:
                warn("No se pudo levantar hci0 (puede requerir sudo)")
        else:
            ok("hci0 UP RUNNING")
    except FileNotFoundError:
        warn("hciconfig no encontrado (puede ser normal en sistemas modernos)")
    except Exception as e:
        warn(f"hciconfig error: {e}")

    # bluetoothctl show
    try:
        r = subprocess.run(["bluetoothctl","show"], capture_output=True, text=True, timeout=5)
        log.info("bluetoothctl show:\n%s", r.stdout.strip())
        if "Powered: yes" in r.stdout:
            ok("Adaptador BT encendido (Powered: yes)")
        else:
            warn("Adaptador BT no encendido. Intentando encender…")
            subprocess.run(["bluetoothctl","power","on"], capture_output=True, timeout=5)
            time.sleep(1)
            r2 = subprocess.run(["bluetoothctl","show"], capture_output=True, text=True)
            if "Powered: yes" in r2.stdout:
                ok("BT encendido con bluetoothctl")
            else:
                err("No se pudo encender BT. Ejecuta:  bluetoothctl power on")
                ok_flag = False
    except FileNotFoundError:
        warn("bluetoothctl no encontrado")
    except Exception as e:
        warn(f"bluetoothctl error: {e}")

    return ok_flag

# ══════════════════════════════════════════════════════════════════════════
# PASO 2 — Escaneo BLE (múltiples intentos)
# ══════════════════════════════════════════════════════════════════════════
async def scan_ble(timeout: float = 10.0, target_hint: str = "") -> list[dict]:
    hdr(f"PASO 2 — Escaneo BLE ({timeout:.0f}s)")
    devices_raw = []
    attempts = [
        ("BleakScanner.discover",      lambda t: BleakScanner.discover(timeout=t)),
        ("BleakScanner (cb detectado)",lambda t: BleakScanner.discover(timeout=t, service_uuids=[])),
    ]
    for name, fn in attempts:
        try:
            log.info("  Intentando: %s …", name)
            devs = await fn(timeout)
            devices_raw = devs
            log.info("  Encontrados %d dispositivos", len(devs))
            break
        except Exception as e:
            warn(f"  {name} falló: {e}")

    result = []
    for d in sorted(devices_raw, key=lambda x: x.rssi if hasattr(x,'rssi') else -99, reverse=True):
        name = d.name or ""
        rssi = d.rssi if hasattr(d, "rssi") else "?"
        is_p = any(k in name.lower() for k in PRINTER_KEYWORDS)
        entry = {"address": d.address, "name": name, "rssi": rssi, "is_printer": is_p, "device": d}
        result.append(entry)
        marker = f"{G}[IMPRESORA]{RST}" if is_p else ""
        log.info("  %s  %-20s  %s  %s dBm", d.address, name or "(sin nombre)", marker, rssi)

    if not result:
        warn("No se encontraron dispositivos BLE. Verifica que la impresora esté encendida.")
    elif not any(e["is_printer"] for e in result):
        warn("Ningún dispositivo parece ser una impresora P15. Lista completa arriba.")

    return result

# ══════════════════════════════════════════════════════════════════════════
# PASO 3 — Resolver objetivo (nombre → MAC)
# ══════════════════════════════════════════════════════════════════════════
async def resolve_target(target: str, scan_results: list[dict]) -> tuple[str, object]:
    """Devuelve (mac, BLEDevice|None)"""
    hdr(f"PASO 3 — Resolver objetivo: {target!r}")

    if MAC_RE.match(target):
        ok(f"Es una MAC directa: {target}")
        dev = next((e["device"] for e in scan_results if e["address"].upper() == target.upper()), None)
        return target, dev

    # buscar por nombre en el escaneo
    target_low = target.lower()
    for e in scan_results:
        if target_low in (e["name"] or "").lower():
            ok(f"Nombre '{target}' → MAC {e['address']}  (nombre completo: {e['name']})")
            return e["address"], e["device"]

    # si no encontramos, buscar la primera impresora detectada
    printers = [e for e in scan_results if e["is_printer"]]
    if printers:
        warn(f"No se encontró '{target}' exactamente. Usando primera impresora detectada: {printers[0]['name']} / {printers[0]['address']}")
        return printers[0]["address"], printers[0]["device"]

    if scan_results:
        warn(f"No se encontró ninguna impresora. ¿El objetivo es uno de estos?")
        for e in scan_results[:5]:
            log.info("    %s  %s", e["address"], e["name"])

    raise RuntimeError(f"No se pudo resolver '{target}' a ningún dispositivo BLE.")

# ══════════════════════════════════════════════════════════════════════════
# PASO 4 — Conectar con reintentos
# ══════════════════════════════════════════════════════════════════════════
def _bluez_remove_device(mac: str):
    """
    Elimina el dispositivo del caché de BlueZ para forzar conexión BLE pura.
    Esto evita el error 'br-connection-profile-unavailable' causado por
    perfiles BR/EDR (SPP) guardados de sesiones anteriores.
    """
    try:
        r = subprocess.run(
            ["bluetoothctl", "remove", mac],
            capture_output=True, text=True, timeout=5
        )
        if "Device has been removed" in r.stdout or "not available" in r.stderr:
            log.info("  Caché BlueZ limpiado para %s", mac)
        else:
            log.info("  bluetoothctl remove: %s %s", r.stdout.strip(), r.stderr.strip())
    except Exception as e:
        warn(f"  No se pudo limpiar caché BlueZ: {e}")


async def connect_with_retries(mac: str, dev=None, max_tries: int = 4) -> BleakClient:
    hdr(f"PASO 4 — Conectar a {mac}")

    # Limpiar caché BlueZ ANTES del primer intento para evitar
    # el error 'br-connection-profile-unavailable'
    log.info("  Limpiando caché BlueZ (evita conflicto BR/EDR ↔ BLE)…")
    _bluez_remove_device(mac)

    # Redescubrir el dispositivo con un escaneo breve para que BlueZ
    # lo registre de nuevo en D-Bus (necesario tras bluetoothctl remove)
    log.info("  Redescubriendo dispositivo con escaneo rápido (5 s)…")
    try:
        found = await BleakScanner.discover(timeout=5.0)
        for d in found:
            if d.address.upper() == mac.upper():
                dev = d
                log.info("  ✔ Dispositivo redescubierto: %s  %s", d.address, d.name)
                break
        else:
            log.warning("  ⚠ Dispositivo no visto en escaneo previo a conexión")
    except Exception as e:
        log.warning("  ⚠ Error en escaneo previo: %s", e)
    await asyncio.sleep(0.5)

    last_err = None
    for attempt in range(1, max_tries + 1):
        log.info("  Intento %d/%d …", attempt, max_tries)
        timeout = 15.0 + attempt * 5
        try:
            # Usar el objeto BLEDevice del scan si está disponible:
            # fuerza transporte LE y evita ambigüedad BR/EDR
            client = BleakClient(dev if dev is not None else mac, timeout=timeout)
            await client.connect()
            if client.is_connected:
                ok(f"Conectado en intento {attempt}  (timeout: {timeout}s)")
                return client
            else:
                warn("  connect() no lanzó excepción pero is_connected=False")
                _bluez_remove_device(mac)
                await asyncio.sleep(2)
        except BleakError as e:
            last_err = e
            warn(f"  BleakError: {e}")
            estr = str(e).lower()
            if "br-connection-profile-unavailable" in estr or "not available" in estr:
                # La excepción viene del perfil SPP clásico, pero la conexión
                # LE puede haberse establecido igualmente — verificar
                if client.is_connected:
                    ok(f"  ⚠ Perfil BR/EDR falló pero LE está conectado — continuando")
                    return client
                warn("  → Limpiando caché BlueZ y reescaneando…")
                _bluez_remove_device(mac)
                await asyncio.sleep(1)
                try:
                    found = await BleakScanner.discover(timeout=5.0)
                    for d in found:
                        if d.address.upper() == mac.upper():
                            dev = d
                            log.info("  ✔ Redescubierto tras br-error: %s", d.address)
                            break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            elif "le-connection-abort-by-local" in estr:
                warn("  → Reset adaptador BT y reintentando…")
                subprocess.run(["sudo","hciconfig","hci0","reset"], capture_output=True)
                await asyncio.sleep(3)
            elif "not found" in estr or "no such device" in estr:
                warn("  → Dispositivo no encontrado en D-Bus; reescaneando…")
                try:
                    found = await BleakScanner.discover(timeout=5.0)
                    for d in found:
                        if d.address.upper() == mac.upper():
                            dev = d
                            log.info("  ✔ Redescubierto tras error: %s", d.address)
                            break
                    else:
                        warn("  ⚠ Impresora no visible. ¿Está encendida?")
                except Exception:
                    pass
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(2)
        except Exception as e:
            last_err = e
            warn(f"  Exception: {e}")
            await asyncio.sleep(2)

    raise RuntimeError(f"No se pudo conectar tras {max_tries} intentos. Último error: {last_err}")

# ══════════════════════════════════════════════════════════════════════════
# PASO 5 — Descubrir servicios y elegir características
# ══════════════════════════════════════════════════════════════════════════
async def discover_services(client: BleakClient) -> tuple[str, str]:
    """Retorna (write_uuid, notify_uuid) válidos."""
    hdr("PASO 5 — Descubriendo servicios y características")

    svcs = client.services
    found_chars = {}

    for svc in svcs:
        log.info("  Servicio: %s", svc.uuid)
        for ch in svc.characteristics:
            props = ",".join(ch.properties)
            log.info("    Característica: %s  [%s]", ch.uuid, props)
            found_chars[ch.uuid.lower()] = ch

    # intentar con los UUIDs conocidos primero
    write_uuid  = None
    notify_uuid = None

    for u in ALT_WRITE_UUIDS:
        if u.lower() in found_chars:
            ch = found_chars[u.lower()]
            if "write" in ch.properties or "write-without-response" in ch.properties:
                write_uuid = u.lower()
                ok(f"UUID escritura: {write_uuid}")
                break

    for u in ALT_NOTIFY_UUIDS:
        if u.lower() in found_chars:
            ch = found_chars[u.lower()]
            if "notify" in ch.properties or "indicate" in ch.properties:
                notify_uuid = u.lower()
                ok(f"UUID notificación: {notify_uuid}")
                break

    # fallback: buscar en todos los encontrados
    if not write_uuid:
        for uuid, ch in found_chars.items():
            if "write" in ch.properties or "write-without-response" in ch.properties:
                write_uuid = uuid
                warn(f"UUID escritura (fallback): {write_uuid}")
                break

    if not notify_uuid:
        for uuid, ch in found_chars.items():
            if "notify" in ch.properties or "indicate" in ch.properties:
                notify_uuid = uuid
                warn(f"UUID notificación (fallback): {notify_uuid}")
                break

    if not write_uuid:
        raise RuntimeError("No se encontró ninguna característica de escritura. Protocolo desconocido.")
    if not notify_uuid:
        warn("No se encontró característica de notificación — continuando sin notificaciones")

    return write_uuid, notify_uuid

# ══════════════════════════════════════════════════════════════════════════
# PASO 6 — Protocolo Niimbot
# ══════════════════════════════════════════════════════════════════════════
def crc(data: bytes) -> int:
    v = 0
    for b in data:
        v ^= b
    return v

def build_packet(cmd: int, data: bytes = b"") -> bytes:
    inner = bytes([cmd, len(data)]) + data
    return b"\x55\x55" + inner + bytes([crc(inner)]) + b"\xAA\xAA"

def split_chunks(packet: bytes, mtu: int = 20) -> list[bytes]:
    return [packet[i:i+mtu] for i in range(0, len(packet), mtu)]

class Printer:
    def __init__(self, client: BleakClient, write_uuid: str, notify_uuid: str | None):
        self.client = client
        self.write_uuid  = write_uuid
        self.notify_uuid = notify_uuid
        self.last_notify: bytes | None = None

    def _on_notify(self, _sender, data: bytearray):
        self.last_notify = bytes(data)
        log.debug("← notify: %s", data.hex())

    async def start_notify(self):
        if self.notify_uuid:
            try:
                await self.client.start_notify(self.notify_uuid, self._on_notify)
                ok("Suscripción a notificaciones OK")
            except Exception as e:
                warn(f"No se pudo suscribir a notificaciones: {e}")

    async def send(self, cmd: int, data: bytes = b"", wait: float = 0.15) -> bytes | None:
        pkt = build_packet(cmd, data)
        log.debug("→ cmd=0x%02X  pkt=%s", cmd, pkt.hex())
        for chunk in split_chunks(pkt):
            try:
                await self.client.write_gatt_char(self.write_uuid, chunk, response=False)
            except Exception:
                # algunos dispositivos requieren response=True
                await self.client.write_gatt_char(self.write_uuid, chunk, response=True)
        await asyncio.sleep(wait)
        return self.last_notify

    async def get_info(self) -> bytes | None:
        resp = await self.send(CMD_GET_INFO, wait=0.5)
        if resp:
            log.info("  Info raw: %s", resp.hex())
        else:
            warn("  Sin respuesta al CMD_GET_INFO (puede ser normal)")
        return resp

    async def print_image(self, img: Image.Image, density: int = 3, copies: int = 1):
        rows = self._image_to_rows(img)
        total = len(rows)
        log.info("  Total filas a imprimir: %d", total)

        await self.send(CMD_SET_LABEL_TYPE, bytes([1]))
        await self.send(CMD_SET_LABEL_DENS, bytes([max(1,min(5,density))]))
        await self.send(CMD_SET_PAGE_SIZE,  struct.pack(">HH", total, PRINT_W_PX))
        await self.send(CMD_ALLOW_PRINT,    struct.pack(">H", copies))
        await self.send(CMD_START_PRINT,    wait=0.3)

        empty = 0
        for i, row in enumerate(rows):
            if all(b == 0 for b in row):
                empty += 1
            else:
                if empty > 0:
                    await self.send(CMD_PRINT_EMPTY, struct.pack(">H", empty), wait=0.02)
                    empty = 0
                await self.send(CMD_PRINT_ROW, row, wait=0.02)
            if (i+1) % 50 == 0:
                log.info("  … fila %d/%d (%.0f%%)", i+1, total, (i+1)/total*100)

        if empty > 0:
            await self.send(CMD_PRINT_EMPTY, struct.pack(">H", empty), wait=0.05)

        await self.send(CMD_END_PRINT, wait=1.0)
        ok("Trabajo de impresión enviado")

    def _image_to_rows(self, img: Image.Image) -> list[bytes]:
        """
        La imagen canvas es 320×96 px (landscape: ancho=largo etiqueta, alto=ancho cinta).
        La impresora avanza por el eje largo (320 px = 40 mm).
        Giramos 90° → queda 96×320, luego enviamos fila a fila (96 px ancho = 12 mm cinta).
        """
        img = img.convert("L")
        img = img.rotate(90, expand=True)                          # 320×96 → 96×320
        img = img.resize((PRINT_W_PX, PRINT_H_PX), Image.LANCZOS) # 96×320 (por si acaso)
        img = img.convert("1")                                     # 1-bit

        row_bytes_n = PRINT_W_PX // 8 + (1 if PRINT_W_PX % 8 else 0)
        rows = []
        for y in range(PRINT_H_PX):
            bits = 0
            for x in range(PRINT_W_PX):
                if img.getpixel((x, y)) == 0:
                    bits |= (1 << (PRINT_W_PX - 1 - x))
            rows.append(bits.to_bytes(row_bytes_n, "big"))
        return rows

# ══════════════════════════════════════════════════════════════════════════
# PASO 7 — Generar imagen de prueba
# ══════════════════════════════════════════════════════════════════════════
def make_test_image() -> Image.Image:
    """
    Genera una etiqueta de prueba en memoria.
    Dimensiones canvas: 320 (largo) × 96 (alto) px — landscape.
    """
    hdr("PASO 7 — Generando imagen de prueba")

    img = Image.new("RGB", (PRINT_H_PX, PRINT_W_PX), "white")   # 320×96 landscape
    draw = ImageDraw.Draw(img)

    # Borde
    draw.rectangle([0, 0, PRINT_H_PX-1, PRINT_W_PX-1], outline="black", width=2)

    # Texto grande "TEST P15"
    try:
        font_big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_sm  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except Exception:
        font_big = ImageFont.load_default()
        font_sm  = font_big

    draw.text((10, 8),  "TEST P15",    fill="black", font=font_big)
    draw.text((10, 42), "12 × 40 mm",  fill="black", font=font_sm)
    draw.text((10, 58), "Niimbot BLE", fill="black", font=font_sm)
    draw.text((10, 73), time.strftime("%d/%m/%Y %H:%M"), fill="black", font=font_sm)

    # Patrón de barras a la derecha (diagnóstico de densidad)
    for i in range(10):
        x = 230 + i * 8
        if i % 2 == 0:
            draw.rectangle([x, 8, x+5, 88], fill="black")

    # Guardar copia local para inspección
    out = Path(__file__).parent / "test_label_preview.png"
    img.save(out)
    ok(f"Imagen de prueba guardada en: {out}")
    return img

# ══════════════════════════════════════════════════════════════════════════
# FLUJO PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════
async def run(target: str, scan_only: bool = False, info_only: bool = False,
              density: int = 3, scan_timeout: float = 10.0):

    log.info("═" * 60)
    log.info("  P15 Designer — Test automatizado")
    log.info("  %s", time.strftime("%Y-%m-%d %H:%M:%S"))
    log.info("═" * 60)

    # 1. Adaptador BT
    bt_ok = check_bluetooth_adapter()
    if not bt_ok:
        warn("El adaptador BT tiene problemas — continuando igualmente (puede funcionar)")

    # 2. Escaneo
    devices = []
    for scan_try in range(1, 4):
        devices = await scan_ble(timeout=scan_timeout, target_hint=target)
        if devices:
            break
        warn(f"Escaneo vacío (intento {scan_try}/3). Esperando 3 s y reintentando…")
        await asyncio.sleep(3)

    if scan_only:
        log.info("\n[scan-only] Fin del escaneo.")
        return

    if not devices and not MAC_RE.match(target):
        raise RuntimeError("Escaneo vacío y el objetivo no es una MAC. No se puede continuar.")

    # 3. Resolver objetivo
    mac, dev = await resolve_target(target, devices)

    # 4. Conectar
    client = await connect_with_retries(mac, dev)

    try:
        # 5. Descubrir servicios
        write_uuid, notify_uuid = await discover_services(client)

        printer = Printer(client, write_uuid, notify_uuid)

        # 6. Notificaciones
        hdr("PASO 6 — Suscribir notificaciones")
        await printer.start_notify()

        # 7. Info
        hdr("PASO 6b — Solicitar info del dispositivo")
        info = await printer.get_info()
        if info:
            ok(f"Info recibida: {info.hex()}")
        else:
            warn("Sin respuesta a GET_INFO — la impresora puede igualmente imprimir")

        if info_only:
            log.info("\n[info-only] Fin.")
            return

        # 8. Imagen de prueba
        img = make_test_image()

        # 9. Imprimir
        hdr("PASO 8 — Enviando trabajo de impresión")
        await printer.print_image(img, density=density, copies=1)

        log.info("\n" + "═"*60)
        ok("TEST COMPLETO — La impresora debería haber impreso la etiqueta.")
        log.info("Si no salió papel: revisa la densidad (--density 4 o 5) y el papel.")
        log.info("Log completo guardado en: %s", LOG_FILE)
        log.info("═"*60)

    finally:
        await client.disconnect()
        log.info("Desconectado.")


# ══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Test automatizado P15 Niimbot BLE")
    parser.add_argument("--target",      default="P15_EEA6_BLE",
                        help="Nombre BLE o MAC de la impresora (default: P15_EEA6_BLE)")
    parser.add_argument("--scan-only",   action="store_true",
                        help="Solo escanear, no conectar")
    parser.add_argument("--info-only",   action="store_true",
                        help="Conectar y leer info, no imprimir")
    parser.add_argument("--density",     type=int, default=3, choices=range(1,6),
                        help="Densidad de impresión 1–5 (default: 3)")
    parser.add_argument("--scan-time",   type=float, default=10.0,
                        help="Segundos de escaneo BLE (default: 10)")
    args = parser.parse_args()

    try:
        asyncio.run(run(
            target      = args.target,
            scan_only   = args.scan_only,
            info_only   = args.info_only,
            density     = args.density,
            scan_timeout= args.scan_time,
        ))
    except KeyboardInterrupt:
        log.info("\n[interrumpido por usuario]")
    except Exception as e:
        err(f"ERROR FATAL: {e}")
        import traceback
        traceback.print_exc()
        log.info("\n── Diagnóstico de error ──")
        msg = str(e).lower()
        if "le-connection-abort" in msg:
            log.info("→ El adaptador canceló la conexión. Prueba:  sudo hciconfig hci0 reset")
        elif "not found" in msg or "no such device" in msg:
            log.info("→ Dispositivo no encontrado. ¿Está encendida la impresora?")
        elif "permission" in msg:
            log.info("→ Permisos insuficientes. Prueba:  sudo python test_printer.py")
        elif "timeout" in msg:
            log.info("→ Timeout de conexión. Acerca la impresora al ordenador.")
        elif "org.bluez" in msg:
            log.info("→ Error de BlueZ/DBus. Prueba:  sudo systemctl restart bluetooth")
        log.info("Log completo en: %s", LOG_FILE)
        sys.exit(1)

if __name__ == "__main__":
    main()
