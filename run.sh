#!/usr/bin/env bash
# run.sh — Lanzador rápido de la aplicación
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv"

if [ ! -d "$VENV" ]; then
  echo "Creando entorno virtual…"
  python3 -m venv "$VENV"
fi

source "$VENV/bin/activate"

# Instalar/actualizar dependencias silenciosamente
pip install -q -r "$DIR/requirements.txt"

echo ""
echo "════════════════════════════════════════"
echo "  Diseñador de Etiquetas P15  12×40 mm"
echo "  http://localhost:5000"
echo "════════════════════════════════════════"
echo ""

python "$DIR/app.py"
