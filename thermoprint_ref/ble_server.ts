/**
 * ble_server.ts — Servidor BLE persistente para Marklife P15
 * ===========================================================
 * Mantiene la conexión BLE activa y expone una API HTTP local en :7315
 *
 * Rutas:
 *   GET  /ping      → { ok, connected }
 *   GET  /status    → { connected, name, id, rssi, status, battery }
 *   POST /print     → body: { imagePath, density, paper, width }
 *   POST /shutdown  → cierra limpiamente
 */

import { Printer, discover, processImage } from "@thermoprint/core";
import type { BlePeripheral, PrintOptions } from "@thermoprint/core";
import { NobleBleTransport } from "./packages/cli/src/transport/noble.js";
import sharp from "sharp";

// ── Config ────────────────────────────────────────────────────────────────────
const HTTP_PORT    = 7315;
const KEEPALIVE_MS = 15_000;
const RECONNECT_MS = 5_000;
const DISCOVER_MS  = 12_000;

// ── Estado ────────────────────────────────────────────────────────────────────
let printer:    Printer | null       = null;
let peripheral: BlePeripheral | null = null;
let keepaliveTimer: ReturnType<typeof setInterval> | null = null;
let reconnectTimer: ReturnType<typeof setTimeout>  | null = null;
let connecting   = false;
let shuttingDown = false;
let lastBattery: number | null = null;

// ── Conexión ──────────────────────────────────────────────────────────────────
async function connect(): Promise<void> {
  if (connecting || shuttingDown) return;
  connecting = true;
  try {
    console.log("[BLE] Buscando impresora…");
    const transport = new NobleBleTransport();
    const found = await discover(transport, { timeoutMs: DISCOVER_MS });
    peripheral = found;
    console.log(`[BLE] Conectando a ${found.name} (${found.id})…`);
    printer = await Printer.connect(transport, found);
    console.log(`[BLE] ✅ Conectado a ${found.name}`);

    printer.on("disconnected", () => {
      if (shuttingDown) return;
      console.warn("[BLE] ⚠ Desconectado — reintentando en 5 s");
      printer = null; peripheral = null;
      stopKeepalive(); scheduleReconnect();
    });

    startKeepalive();
  } catch (err: any) {
    console.error("[BLE] ❌ Error:", err.message);
    printer = null; peripheral = null;
    scheduleReconnect();
  } finally {
    connecting = false;
  }
}

function scheduleReconnect() {
  if (shuttingDown) return;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, RECONNECT_MS);
}

function startKeepalive() {
  stopKeepalive();
  keepaliveTimer = setInterval(async () => {
    if (!printer || shuttingDown) return;
    try {
      lastBattery = await printer.getBattery();
      console.log(`[BLE] Keepalive — batería: ${lastBattery}%`);
    } catch (e: any) {
      console.warn("[BLE] Keepalive falló:", e.message);
    }
  }, KEEPALIVE_MS);
}

function stopKeepalive() {
  if (keepaliveTimer) { clearInterval(keepaliveTimer); keepaliveTimer = null; }
}

async function shutdown() {
  shuttingDown = true;
  stopKeepalive();
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (printer) {
    try { await printer.disconnect(); console.log("[BLE] Desconectado limpiamente"); }
    catch { /* ignorar */ }
    printer = null;
  }
}

process.on("SIGINT",  async () => { await shutdown(); process.exit(0); });
process.on("SIGTERM", async () => { await shutdown(); process.exit(0); });

// ── HTTP Server ───────────────────────────────────────────────────────────────
const server = Bun.serve({
  port: HTTP_PORT,

  async fetch(req: Request): Promise<Response> {
    const { pathname } = new URL(req.url);

    if (pathname === "/ping") {
      return Response.json({ ok: true, connected: printer !== null });
    }

    if (pathname === "/status" && req.method === "GET") {
      if (!printer || !peripheral) {
        return Response.json({ connected: false, error: connecting ? "Conectando…" : "Sin conexión" });
      }
      try {
        const [status, battery] = await Promise.all([printer.getStatus(), printer.getBattery()]);
        lastBattery = battery;
        return Response.json({
          connected: true,
          name: peripheral.name,
          id:   peripheral.id,
          rssi: peripheral.rssi,
          status: status.status,
          battery,
        });
      } catch (e: any) {
        return Response.json({ connected: false, error: e.message });
      }
    }

    if (pathname === "/print" && req.method === "POST") {
      if (!printer) {
        return Response.json({ ok: false, error: "Impresora no conectada" }, { status: 503 });
      }
      let body: any;
      try { body = await req.json(); } catch {
        return Response.json({ ok: false, error: "JSON inválido" }, { status: 400 });
      }
      const { imagePath, density = 3, paper = "gap", width = 384 } = body;
      if (!imagePath) return Response.json({ ok: false, error: "Falta imagePath" }, { status: 400 });

      try {
        const raw = await sharp(imagePath)
          .resize(width, null, { fit: "contain", background: { r: 255, g: 255, b: 255, alpha: 1 } })
          .ensureAlpha()
          .raw()
          .toBuffer({ resolveWithObject: true });

        const imgData = {
          data:   new Uint8ClampedArray(raw.data),
          width:  raw.info.width,
          height: raw.info.height,
        };

        const bitmap = processImage(imgData, { dither: "floyd-steinberg", width });
        await printer!.printBitmap(bitmap, {
          density:   Math.max(1, Math.min(3, density)) as 1 | 2 | 3,
          paperType: paper as "gap" | "continuous",
        });
        return Response.json({ ok: true, message: "✔ Impreso correctamente" });
      } catch (e: any) {
        console.error("[BLE] Error al imprimir:", e.message);
        return Response.json({ ok: false, error: e.message }, { status: 500 });
      }
    }

    if (pathname === "/shutdown" && req.method === "POST") {
      setTimeout(async () => { await shutdown(); server.stop(); process.exit(0); }, 200);
      return Response.json({ ok: true });
    }

    return Response.json({ error: "Not found" }, { status: 404 });
  },
});

console.log(`[BLE Server] Puerto ${HTTP_PORT} listo`);
console.log("[BLE Server] Conectando a la impresora…");
connect();
