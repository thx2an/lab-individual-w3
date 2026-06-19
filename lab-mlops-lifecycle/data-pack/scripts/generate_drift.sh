#!/usr/bin/env bash
# generate_drift.sh — Tái tạo baseline.csv và drifted.csv từ generate_data.py
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$SCRIPT_DIR/../data"

echo "[generate_drift] Generating baseline.csv and drifted.csv..."
uv run python "$DATA_DIR/generate_data.py" --output-dir "$DATA_DIR"

echo "[generate_drift] Done."
echo "[generate_drift]   baseline.csv : $(wc -l < "$DATA_DIR/baseline.csv") rows"
echo "[generate_drift]   drifted.csv  : $(wc -l < "$DATA_DIR/drifted.csv") rows"
