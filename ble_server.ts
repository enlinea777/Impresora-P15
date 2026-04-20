/**
 * ble_server.ts — Servidor BLE persistente para Marklife P15
 * ===========================================================
 * • Conecta al arrancar y mantiene la conexión viva (keepalive cada 15 s)
 * • Si se cae la conexión, reintenta automáticamente cada 5 s
 * • Expone una API HTTP en http://localhost:7315
 *   GET  /status          → { connected, name, id, rssi, status, battery }
 *   POST /print           → { imagePath, density, paper, width } → { ok, message }
 *   POST /shutdown        → cierra el servidor limpiamente
 *
 * Inicio:   bun run ble_server.ts
 * Detener:  POST http://localhost:7315/shutdown   (o Ctrl+C)
 */

import { Printer, discover, processImage } from "@thermoprint/core";
import type { BlePeripheral, PrintOptions } from "@thermoprint/core";
import { NobleBleTransport } from "./thermoprint_ref/packages/cli/src/transport/noble.js";

// ── Config ────────────────────────────────────────────────────────────────────
const HTTP_PORT      = 7315;
const KEEPALIVE_MS   = 15_000;   // ping de batería cada 15 s
const RECONNECT_MS   = 5_000;    // reintento de conexión cada 5 s
const DISCOVER_MS    = 10_000;   // timeout de descubrimiento

// ── Estado global ─────────────────────────────────────────────────────────────
let printer:    Printer | null      = null;
let peripheral: BlePeripheral | null = null;
let transport:  NobleBleTransport | null = null;
let keepaliveTimer: ReturnType<typeof setInterval> | null = null;
let reconnectTimer: ReturnType<typeof setTimeout>  | null = null;
let connecting = false;
let shuttingDown = false;

// ── Conexión / reconexión ─────────────────────────────────────────────────────
async function connect(): Promise<void> {
  if (connecting || shuttingDown) return;
  connecting = true;

  try {
    console.log("[BLE] Buscando impresora…");
    transport = new NobleBleTransport();
    const found = await discover(transport, { timeoutMs: DISCOVER_MS });
    peripheral = found;

    console.log(`[BLE] Conectando a ${found.name} (${found.id})…`);
    printer = await Printer.connect(transport, found);
    console.log(`[BLE] ✅ Conectado a ${found.name}`);

    // Escuchar desconexión inesperada
    printer.on("disconnected", () => {
      if (shuttingDown) return;
      console.warn("[BLE] ⚠ Desconectado inesperadamente — reintentando en 5 s");
      printer    = null;
      peripheral = null;
      stopKeepalive();
      scheduleReconnect();
    });

    startKeepalive();
  } catch (err: any) {
    console.error("[BLE] ❌ Error al conectar:", err.message);
    printer    = null;
    peripheral = null;
    scheduleReconnect();
  } finally {
    connecting = false;
  }
}

function scheduleReconnect(): void {
  if (shuttingDown) return;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, RECONNECT_MS);
}

// ── Keepalive ─────────────────────────────────────────────────────────────────
function startKeepalive(): void {
  stopKeepalive();
  keepaliveTimer = setInterval(async () => {
    if (!printer || shuttingDown) return;
    try {
      const batt = await printer.getBattery();
      console.log(`[BLE] Keepalive OK — batería: ${batt}%`);
    } catch (err: any) {
      console.warn("[BLE] Keepalive falló:", err.message);
      // La desconexión inesperada se maneja en el evento "disconnected"
    }
  }, KEEPALIVE_MS);
}

function stopKeepalive(): void {
  if (keepaliveTimer) { clearInterval(keepaliveTimer); keepaliveTimer = null; }
}

// ── Apagado limpio ────────────────────────────────────────────────────────────
async function shutdown(): Promise<void> {
  shuttingDown = true;
  stopKeepalive();
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (printer) {
    try { await printer.disconnect(); console.log("[BLE] Desconectado limpiamente"); }
    catch { /* ignorar errores al cerrar */ }
    printer = null;
  }
}

process.on("SIGINT",  async () => { await shutdown(); process.exit(0); });
process.on("SIGTERM", async () => { await shutdown(); process.exit(0); });

// ── HTTP Server ───────────────────────────────────────────────────────────────
const server = Bun.serve({
  port: HTTP_PORT,

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);

    // ── GET /status ───────────────────────────────────────────────────────────
    if (url.pathname === "/status" && req.method === "GET") {
      if (!printer || !peripheral) {
        return Response.json({ connected: false, error: "No conectada" });
      }
      try {
        const [status, battery] = await Promise.all([
          printer.getStatus(),
          printer.getBattery(),
        ]);
        return Response.json({
          connected: true,
          name:    peripheral.name,
          id:      peripheral.id,
          rssi:    peripheral.rssi,
          status:  status.status,
          battery,
        });
      } catch (err: any) {
        return Response.json({ connected: false, error: err.message });
      }
    }

    // ── POST /print ───────────────────────────────────────────────────────────
    if (url.pathname === "/print" && req.method === "POST") {
      if (!printer) {
        return Response.json({ ok: false, error: "Impresora no conectada" }, { status: 503 });
      }
      let body: any;
      try { body = await req.json(); }
      catch { return Response.json({ ok: false, error: "JSON inválido" }, { status: 400 }); }

      const imagePath: string  = body.imagePath;
      const density:   number  = body.density  ?? 3;
      const paper:     string  = body.paper     ?? "gap";
      const width:     number  = body.width     ?? 384;

      if (!imagePath) {
        return Response.json({ ok: false, error: "Se requiere imagePath" }, { status: 400 });
      }

      try {
        // Leer y procesar imagen con sharp (igual que el CLI)
        const sharp = (await import("sharp")).default;
        const raw = await sharp(imagePath)
          .resize(width, null, { fit: "contain", background: { r: 255, g: 255, b: 255, alpha: 1 } })
          .raw()
          .toBuffer({ resolveWithObject: true });

        const imgData = {
          data:   new Uint8ClampedArray(raw.data),
          width:  raw.info.width,
          height: raw.info.height,
        };

        const processed = processImage(imgData, {
          dither: "floyd-steinberg",
          width,
        });

        const opts: PrintOptions = {
          density:   density as 1 | 2 | 3,
          paperType: paper as "gap" | "continuous",
        };

        await printer!.printBitmap(processed, opts);
        return Response.json({ ok: true, message: "✔ Impreso correctamente" });
      } catch (err: any) {
        console.error("[BLE] Error al imprimir:", err.message);
        return Response.json({ ok: false, error: err.message }, { status: 500 });
      }
    }

    // ── POST /shutdown ────────────────────────────────────────────────────────
    if (url.pathname === "/shutdown" && req.method === "POST") {
      console.log("[BLE] Shutdown solicitado vía HTTP");
      setTimeout(async () => {
        await shutdown();
        server.stop();
        process.exit(0);
      }, 200);
      return Response.json({ ok: true, message: "Cerrando…" });
    }

    // ── GET /ping ─────────────────────────────────────────────────────────────
    if (url.pathname === "/ping") {
      return Response.json({ ok: true, connected: printer !== null });
    }

    return Response.json({ error: "Not found" }, { status: 404 });
  },
});

console.log(`[BLE Server] Escuchando en http://localhost:${HTTP_PORT}`);
console.log("[BLE Server] Iniciando conexión a la impresora…");

// Conectar al arrancar
connect();
