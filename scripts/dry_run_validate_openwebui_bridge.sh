#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bash "$ROOT_DIR/scripts/manage_openwebui_bridge.sh" preview
bash "$ROOT_DIR/scripts/manage_openwebui_bridge.sh" config >/dev/null

echo "phase5 dry-run OK"