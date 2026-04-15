#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/load_openwebui_bridge_env.sh
source "$ROOT_DIR/scripts/load_openwebui_bridge_env.sh"

if [[ -z "${XIAMIMATE_BASELINE_ROOT:-}" ]]; then
    echo "XIAMIMATE_BASELINE_ROOT is required"
    exit 1
fi

SOURCE_DIR="$XIAMIMATE_BASELINE_ROOT/open_webui/data/open-webui/cache/embedding"
TARGET_DIR="$ROOT_DIR/data/open-webui/cache/embedding"

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "baseline embedding cache not found: $SOURCE_DIR"
    exit 1
fi

mkdir -p "$(dirname "$TARGET_DIR")"
rm -rf "$TARGET_DIR"
cp -R "$SOURCE_DIR" "$TARGET_DIR"

echo "seeded $TARGET_DIR from $SOURCE_DIR"