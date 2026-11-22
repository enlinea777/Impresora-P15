#!/usr/bin/env bash
# build_appimage.sh — genera un AppImage autocontenido de P15 Designer
# ==========================================================================
# Incluye: Python standalone, dependencias pip, bun y las dependencias Node de
# thermoprint. No requiere Python ni bun instalados para ejecutar el resultado.
#
# Uso:
#   ./packaging/build_appimage.sh
#
# Variables opcionales:
#   P15_VERSION         versión mostrada en el nombre (por defecto 1.0.0)
#   P15_PY_VERSION      versión de Python standalone (3.12.16)
#   P15_PBS_TAG         release de python-build-standalone (20261110)
#   P15_BUN_VERSION     versión de bun (1.3.2)
#   P15_SKIP_NODE=1     reutiliza build/thermoprint sin ejecutar bun install
#
# Salida:
#   dist/P15-Designer-<version>-<arch>.AppImage
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── Arquitectura ─────────────────────────────────────────────────────────────
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64)
    ARCH="x86_64"
    PBS_ARCH="x86_64-unknown-linux-gnu"
    BUN_ARCH="x64"
    ;;
  aarch64|arm64)
    ARCH="aarch64"
    PBS_ARCH="aarch64-unknown-linux-gnu"
    BUN_ARCH="arm64"
    ;;
  *)
    echo "❌ Arquitectura no soportada: $ARCH" >&2
    exit 1
    ;;
esac

VERSION="${P15_VERSION:-1.0.0}"
PY_VERSION="${P15_PY_VERSION:-3.12.16}"
PBS_TAG="${P15_PBS_TAG:-20261110}"
BUN_VERSION="${P15_BUN_VERSION:-1.3.2}"

CACHE="$ROOT/.build-cache"
BUILD="$ROOT/build"
APPDIR="$BUILD/AppDir"
DIST="$ROOT/dist"
STAGE="$BUILD/thermoprint"
mkdir -p "$CACHE" "$DIST"

log() { printf '\n\033[1;34m▶ %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m❌ %s\033[0m\n' "$*" >&2; exit 1; }

fetch() {
  local url="$1" dest="$2"
  if [ -f "$dest" ]; then
    echo "   (en caché) $(basename "$dest")"
    return
  fi
  echo "   Descargando $(basename "$dest")…"
  curl -fL --retry 3 --connect-timeout 20 -o "$dest.part" "$url"
  mv "$dest.part" "$dest"
}

# ── 1. Python standalone ─────────────────────────────────────────────────────
PBS_NAME="cpython-${PY_VERSION}+${PBS_TAG}-${PBS_ARCH}-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_NAME}"
PBS_TGZ="$CACHE/$PBS_NAME"

if [ ! -x "$APPDIR/usr/lib/p15/python/bin/python3" ]; then
  log "Python standalone $PY_VERSION"
  fetch "$PBS_URL" "$PBS_TGZ"
  rm -rf "$APPDIR" && mkdir -p "$APPDIR/usr/lib/p15"
  tar -xzf "$PBS_TGZ" -C "$APPDIR/usr/lib/p15"
fi

PY="$APPDIR/usr/lib/p15/python/bin/python3"
[ -x "$PY" ] || fail "No se pudo preparar Python standalone"

log "Instalando dependencias de Python"
# -s + PYTHONNOUSERSITE: no reutilizar paquetes de ~/.local del sistema.
# Se usa el lock de versiones exactas si existe (build reproducible).
REQ="$ROOT/packaging/requirements.lock.txt"
[ -f "$REQ" ] || REQ="$ROOT/requirements.txt"
PYTHONNOUSERSITE=1 "$PY" -s -m pip install --disable-pip-version-check \
  --no-warn-script-location -r "$REQ"

# ── 2. bun ──────────────────────────────────────────────────────────────────
log "bun $BUN_VERSION"
BUN_ZIP="$CACHE/bun-linux-${BUN_ARCH}-${BUN_VERSION}.zip"
fetch "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-${BUN_ARCH}.zip" "$BUN_ZIP"
rm -rf "$CACHE/bun-extract"
mkdir -p "$CACHE/bun-extract"
unzip -q -o "$BUN_ZIP" -d "$CACHE/bun-extract"
BUN_SRC="$(find "$CACHE/bun-extract" -type f -name bun | head -1)"
[ -n "$BUN_SRC" ] || fail "No se encontró bun dentro de $BUN_ZIP"
mkdir -p "$APPDIR/usr/lib/p15/thermoprint"
install -m 755 "$BUN_SRC" "$APPDIR/usr/lib/p15/thermoprint/bun"
BUNDLED_BUN="$APPDIR/usr/lib/p15/thermoprint/bun"

# ── 3. Código de la app ─────────────────────────────────────────────────────
log "Copiando código de la aplicación"
APP_DST="$APPDIR/usr/lib/p15/app"
rm -rf "$APP_DST"
mkdir -p "$APP_DST"
for item in app.py printer.py tray_app.py templates static; do
  cp -a "$ROOT/$item" "$APP_DST/"
done

# ── 4. Dependencias Node (solo workspace cli) ───────────────────────────────
log "Instalando dependencias Node con bun"
rm -rf "$STAGE"
mkdir -p "$STAGE"
rsync -a \
  --exclude 'node_modules' \
  --exclude '.git' \
  --exclude '*.log' \
  "$ROOT/thermoprint_ref/" "$STAGE/"

if [ "${P15_SKIP_NODE:-0}" != "1" ]; then
  ( cd "$STAGE" && "$BUNDLED_BUN" install --production --filter '@thermoprint/cli' )
else
  echo "   (P15_SKIP_NODE=1: se omite bun install)"
fi

# Quitar binarios de sharp para musl: el AppImage usa glibc.
rm -rf "$STAGE"/node_modules/.bun/@img+*linuxmusl* || true
# Quitar prebuilds de noble para otras plataformas.
if [ -d "$STAGE/node_modules/.bun" ]; then
  while IFS= read -r d; do
    case "$d" in
      */prebuilds/linux-x64|*/prebuilds) : ;;
      */prebuilds/*) rm -rf "$d" ;;
    esac
  done < <(find "$STAGE/node_modules/.bun" -mindepth 1 -maxdepth 6 -type d -path '*/prebuilds/*')
fi

[ -d "$STAGE/node_modules" ] || fail "Falta node_modules tras bun install"
TH_DST="$APPDIR/usr/lib/p15/thermoprint"
rm -rf "$TH_DST"
mkdir -p "$TH_DST"
cp -a "$STAGE/." "$TH_DST/"
install -m 755 "$BUN_SRC" "$TH_DST/bun"
rm -rf "$TH_DST/node_modules/.cache" || true

# ── 5. Integración de escritorio ────────────────────────────────────────────
log "Preparando AppDir"
mkdir -p "$APPDIR/usr/lib/p15/bin"
install -m 755 "$ROOT/packaging/AppRun" "$APPDIR/AppRun"
install -m 755 "$ROOT/packaging/setup-bluetooth.sh" "$APPDIR/usr/lib/p15/bin/setup-bluetooth"
install -m 644 "$ROOT/packaging/p15-designer.desktop" "$APPDIR/p15-designer.desktop"

rm -rf "$APPDIR/usr/share"
ICON_DIR="$APPDIR/usr/share/icons/hicolor/512x512/apps"
META_DIR="$APPDIR/usr/share/metainfo"
APPS_DIR="$APPDIR/usr/share/applications"
mkdir -p "$ICON_DIR" "$META_DIR" "$APPS_DIR"
install -m 644 "$ROOT/packaging/p15-designer.png" "$ICON_DIR/p15-designer.png"
install -m 644 "$ROOT/packaging/p15-designer.png" "$APPDIR/p15-designer.png"
install -m 644 "$ROOT/packaging/p15-designer.png" "$APPDIR/.DirIcon"
install -m 644 "$ROOT/packaging/io.github.p15designer.appdata.xml" \
  "$META_DIR/io.github.p15designer.appdata.xml"
install -m 644 "$ROOT/packaging/p15-designer.desktop" \
  "$APPS_DIR/p15-designer.desktop"

printf '%s\n' "$VERSION" > "$APPDIR/usr/lib/p15/VERSION"

# Evitar advertencias de appimagetool por permisos no legibles por terceros
chmod -R a+rX "$APPDIR"

# ── 6. Empaquetar con appimagetool ──────────────────────────────────────────
log "Empaquetando AppImage"
AT="$CACHE/appimagetool-$ARCH.AppImage"
fetch "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage" "$AT"
chmod +x "$AT"

AT_DIR="$CACHE/appimagetool-$ARCH-extracted"
if [ ! -x "$AT_DIR/AppRun" ]; then
  rm -rf "$AT_DIR" "$CACHE/squashfs-root"
  ( cd "$CACHE" && "$AT" --appimage-extract >/dev/null )
  mv "$CACHE/squashfs-root" "$AT_DIR"
fi

OUT="$DIST/P15-Designer-${VERSION}-${ARCH}.AppImage"
rm -f "$OUT"
ARCH="$ARCH" "$AT_DIR/AppRun" "$APPDIR" "$OUT" >/dev/null

chmod +x "$OUT"
log "Listo"
printf '   %s\n   %s\n' "$OUT" "$(du -h "$OUT" | cut -f1)"
cat <<EOF

Ejecuta:
  "$OUT"

Si el Bluetooth no conecta la primera vez:
  "$OUT" --setup-bluetooth

También puedes usar el botón «Permitir Bluetooth» dentro de la app.
EOF
