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

import { Printer, discover, processImage, exportDebugLog } from "@thermoprint/core";
import type { BlePeripheral, PrintOptions } from "@thermoprint/core";
import { NobleBleTransport } from "./transport/noble.js";
import sharp from "sharp";

// ── Config ────────────────────────────────────────────────────────────────────
const HTTP_PORT    = Number(process.env.P15_BLE_PORT ?? process.env.BLE_SERVER_PORT ?? 7315);
const KEEPALIVE_MS = 15_000;
const RECONNECT_MS = 5_000;
const DISCOVER_MS  = 12_000;
const QUERY_TIMEOUT_MS = 4_000;   // timeout para getBattery / getStatus

// Envuelve una promesa con un timeout — resuelve null si supera el límite
function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T | null> {
  return Promise.race([
    promise,
    new Promise<null>((resolve) => setTimeout(() => resolve(null), ms)),
  ]);
}

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

/** Espera a que haya una conexión BLE activa, forzando reconexión si hace falta. */
function waitForConnection(timeoutMs = 15_000): Promise<boolean> {
  return new Promise((resolve) => {
    const startedAt = Date.now();
    if (!printer && !connecting) scheduleReconnect();
    const tick = () => {
      if (printer) return resolve(true);
      if (Date.now() - startedAt >= timeoutMs) return resolve(false);
      setTimeout(tick, 250);
    };
    tick();
  });
}

function startKeepalive() {
  stopKeepalive();
  keepaliveTimer = setInterval(async () => {
    if (!printer || shuttingDown) return;
        // Intentar getBattery para mantener viva la conexión BLE
    try {
      const b = await withTimeout(printer.getBattery(), QUERY_TIMEOUT_MS);
      if (b !== null) { lastBattery = b; console.log(`[BLE] Keepalive OK — batería: ${b}%`); }
    } catch {
      // La P15 no siempre responde — la conexión puede seguir activa
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
      // Consultas opcionales — la P15 no siempre responde, no bloquear
      let battery: number | null = lastBattery;
      let statusStr = "ready";
      try { const b = await withTimeout(printer.getBattery(), QUERY_TIMEOUT_MS); if (b !== null) { battery = b; lastBattery = b; } } catch { /* usar lastBattery */ }
      try { const s = await withTimeout(printer.getStatus(), QUERY_TIMEOUT_MS); if (s !== null) statusStr = s.status; } catch { /* ignorar */ }
      return Response.json({
        connected: true,
        name: peripheral.name,
        id:   peripheral.id,
        rssi: peripheral.rssi,
        status: statusStr,
        battery,
      });
    }

    if (pathname === "/print" && req.method === "POST") {
      if (!printer) {
        const reconnected = await waitForConnection();
        if (!reconnected) {
          return Response.json({ ok: false, error: "Impresora no conectada por BLE" }, { status: 503 });
        }
      }
      let body: any;
      try { body = await req.json(); } catch {
        return Response.json({ ok: false, error: "JSON inválido" }, { status: 400 });
      }
      const { imagePath, density = 3, paper = "gap", width = 384, postFeedDots = 0, pauseBeforePostMs = 0, pauseBeforeStopMs = 0, skip = [] } = body;
      if (!imagePath) return Response.json({ ok: false, error: "Falta imagePath" }, { status: 400 });

      try {
        // Rotar 90° si la imagen es landscape (ancho > alto)
        // La imagen de Python es label_w×label_h px (ej. 320×96)
        // Tras rotar queda label_h×label_w (ej. 96×320) = cabezal × avance
        const meta = await sharp(imagePath).metadata();
        const isLandscape = (meta.width ?? 0) > (meta.height ?? 0);

        const { data, info } = await sharp(imagePath)
          .rotate(isLandscape ? 90 : 0)
          .ensureAlpha()
          .raw()
          .toBuffer({ resolveWithObject: true });

        const imgData = {
          data:   new Uint8Array(data.buffer, data.byteOffset, data.byteLength),
          width:  info.width,
          height: info.height,
        };

        const bitmap = processImage(imgData, { dither: "floyd-steinberg" });
        await printer!.printBitmap(bitmap, {
          density:      Math.max(1, Math.min(3, density)) as 1 | 2 | 3,
          paperType:    paper as "gap" | "continuous",
          postFeedDots: postFeedDots,
          pauseBeforePostMs: Number(pauseBeforePostMs) || 0,
          pauseBeforeStopMs: Number(pauseBeforeStopMs) || 0,
          skip:         skip as string[],
        });
        return Response.json({ ok: true, message: "✔ Impreso correctamente" });
      } catch (e: any) {
        console.error("[BLE] Error al imprimir:", e.message);
        return Response.json({ ok: false, error: e.message }, { status: 500 });
      }
    }

    if (pathname === "/debug" && req.method === "GET") {
      return new Response(exportDebugLog(), { headers: { "Content-Type": "text/plain; charset=utf-8" } });
    }

    if (pathname === "/send" && req.method === "POST") {
      if (!printer) {
        return Response.json({ ok: false, error: "Impresora no conectada" }, { status: 503 });
      }
      let body: any;
      try { body = await req.json(); } catch {
        return Response.json({ ok: false, error: "JSON inválido" }, { status: 400 });
      }
      const hex = String(body.hex ?? "").replace(/\s+/g, "");
      if (!hex || hex.length % 2 !== 0 || /[^0-9a-fA-F]/.test(hex)) {
        return Response.json({ ok: false, error: "hex inválido" }, { status: 400 });
      }
      const bytes = Uint8Array.from(hex.match(/../g)!.map((h) => parseInt(h, 16)));
      try {
        await printer.sendRaw(bytes);
        return Response.json({ ok: true, sent: bytes.length });
      } catch (e: any) {
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
