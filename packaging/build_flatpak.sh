#!/usr/bin/env bash
# build_flatpak.sh — empaqueta el bundle Linux como Flatpak
# ==========================================================
# Requiere flatpak y flatpak-builder. En CI se instalan previamente.
#
# Uso:
#   python3 packaging/build_bundle.py --no-archive
#   ./packaging/build_flatpak.sh
#
# El resultado es dist/P15-Designer-<version>-<arch>.flatpak
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${P15_VERSION:-1.0.0}"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) ARCH="x86_64" ;;
  aarch64|arm64) ARCH="aarch64" ;;
  *) echo "❌ Arquitectura no soportada: $ARCH" >&2; exit 1 ;;
esac

BUNDLE_DIR="${1:-$ROOT/build/bundle/P15-Designer}"
APP_ID="io.github.enlinea777.ImpresoraP15"
WORK="$ROOT/build/flatpak-manifest"
REPO="$ROOT/build/flatpak-repo"
BUILD_DIR="$ROOT/build/flatpak-build"
OUT="$ROOT/dist/P15-Designer-$VERSION-$ARCH.flatpak"

command -v flatpak-builder >/dev/null 2>&1 || {
  echo "❌ Falta flatpak-builder. Instala: sudo apt install flatpak flatpak-builder" >&2
  exit 1
}
[ -d "$BUNDLE_DIR" ] || {
  echo "❌ No existe el bundle: $BUNDLE_DIR" >&2
  echo "   Ejecuta antes: python3 packaging/build_bundle.py --no-archive" >&2
  exit 1
}

rm -rf "$WORK" "$REPO" "$BUILD_DIR"
mkdir -p "$WORK" "$ROOT/dist"
cp -a "$ROOT/packaging/flatpak/." "$WORK/"
cp -a "$ROOT/packaging/p15-designer.png" "$WORK/p15-designer.png"
cp -a "$BUNDLE_DIR" "$WORK/p15"

echo "▶ Compilando Flatpak $APP_ID ($ARCH)…"
INSTALL_DEPS=""
if [ "${P15_FLATPAK_INSTALL_DEPS:-0}" = "1" ]; then
  INSTALL_DEPS="--install-deps-from=flathub"
fi

flatpak-builder --user --force-clean $INSTALL_DEPS \
  --arch="$ARCH" --repo="$REPO" "$BUILD_DIR" \
  "$WORK/$APP_ID.yml"

rm -f "$OUT"
flatpak build-bundle --arch="$ARCH" "$REPO" "$OUT" "$APP_ID" \
  --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo

echo "▶ Listo: $OUT ($(du -h "$OUT" | cut -f1))"
