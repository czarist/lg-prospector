#!/usr/bin/env bash
# Move logs do SSD (projeto) para o HD e deixa o app gravar lá.
# Uso (na pasta do projeto):
#   ./scripts/move_logs_to_hdd.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${LOGS_DIR:-/storage/lg-prospector/logs}"
SRC="$ROOT/logs"

echo "destino: $DEST"
echo "origem:  $SRC"

echo "→ criando pasta no HD (sudo)…"
sudo mkdir -p "$DEST"
sudo chown -R "$(id -un):$(id -gn)" "$(dirname "$DEST")" "$DEST"

if [[ -d "$SRC" && ! -L "$SRC" ]]; then
  echo "→ parando workers pra não gravar no meio da cópia…"
  "$ROOT/.venv/bin/python" "$ROOT/scripts/cockpit.py" --stop || true
  echo "→ copiando logs…"
  rsync -a --info=stats2 "$SRC"/ "$DEST"/
  echo "→ trocando pasta do projeto por symlink…"
  BACKUP="$ROOT/logs.ssd.bak"
  rm -rf "$BACKUP"
  mv "$SRC" "$BACKUP"
  ln -s "$DEST" "$SRC"
  echo "→ backup local em $BACKUP (pode apagar depois de conferir)"
elif [[ -L "$SRC" ]]; then
  echo "já é symlink → $(readlink -f "$SRC")"
else
  echo "sem logs locais; só a pasta no HD"
fi

ENV="$ROOT/.env"
if [[ -f "$ENV" ]] && grep -qE '^LOGS_DIR=' "$ENV"; then
  sed -i "s|^LOGS_DIR=.*|LOGS_DIR=$DEST|" "$ENV"
else
  printf '\nLOGS_DIR=%s\n' "$DEST" >> "$ENV"
fi

echo
echo "ok. logs no HD: $DEST"
echo "suba de novo:  cd $ROOT && ./cockpit"
echo "espaço: $(du -sh "$DEST" | awk '{print $1}')"
