#!/bin/zsh
set -euo pipefail

PROJECT_DIR="/Users/antonokrut/Documents/New project"
VENV_BIN="$PROJECT_DIR/.venv/bin"

DATE="$(date +%Y-%m-%d)"

cd "$PROJECT_DIR"
"$VENV_BIN/python" manage.py poster_import_daily --date "$DATE" --include-products-sales
"$VENV_BIN/python" manage.py report_anomalies --date "$DATE"
