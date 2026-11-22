#!/usr/bin/env bash
# setup-bluetooth.sh — concede cap_net_raw/cap_net_admin a bun (Linux)
#
# En el AppImage el binario vive en una imagen de solo lectura, así que se
# copia a ~/.local/share/p15-designer/bin/bun y se le aplican file capabilities.
set -euo pipefail

HERE="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
DATA_DIR="${P15_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/p15-designer}"

if [ -n "${P15_THERMOPRINT_DIR:-}" ]; then
  SRC="$P15_THERMOPRINT_DIR/bun"
elif [ -f "$HERE/../thermoprint/bun" ]; then
  SRC="$HERE/../thermoprint/bun"
else
  SRC="$(command -v bun || true)"
fi

if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
  echo "❌ No se encontró el binario de bun." >&2
  exit 1
fi

DST="$DATA_DIR/bin/bun"
mkdir -p "$DATA_DIR/bin"
echo "Copiando bun a $DST…"
cp -f "$SRC" "$DST"
chmod 755 "$DST"

if [ "$(id -u)" -eq 0 ]; then
  setcap cap_net_raw,cap_net_admin+eip "$DST"
elif [ -t 0 ] && command -v sudo >/dev/null 2>&1; then
  sudo setcap cap_net_raw,cap_net_admin+eip "$DST"
elif command -v pkexec >/dev/null 2>&1; then
  pkexec setcap cap_net_raw,cap_net_admin+eip "$DST"
else
  echo "❌ No hay sudo ni pkexec. Ejecuta como root:" >&2
  echo "   setcap cap_net_raw,cap_net_admin+eip '$DST'" >&2
  exit 1
fi

if command -v getcap >/dev/null 2>&1; then
  echo "Permisos actuales: $(getcap "$DST")"
fi
echo "✅ Permisos Bluetooth configurados."
echo "   Reinicia P15 Designer para reconectar la impresora."
