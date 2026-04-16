#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.yml"
# shellcheck source=scripts/load_openwebui_bridge_env.sh
source "$ROOT_DIR/scripts/load_openwebui_bridge_env.sh"

require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "docker is required"
        return 1
    fi
}

compose_cmd() {
    require_docker
    docker compose -f "$COMPOSE_FILE" "$@"
}

preview_bridge() {
    echo "compose_file=$COMPOSE_FILE"
    echo "open_webui_url=http://127.0.0.1:${OPEN_WEBUI_PORT}"
    echo "pipelines_url=http://127.0.0.1:${PIPELINES_PORT}"
    echo "chat_backend_base_url=$CHAT_BACKEND_BASE_URL"
    echo "model_prefix=$XIAMIMATE_MODEL_PREFIX"
    echo "images=${OPEN_WEBUI_NGINX_IMAGE} | ${OPEN_WEBUI_IMAGE} | ${PIPELINES_IMAGE}"
    echo "baseline_root=${XIAMIMATE_BASELINE_ROOT:-}"
}

config_bridge() {
    compose_cmd config
}

up_bridge() {
    compose_cmd up -d
}

down_bridge() {
    compose_cmd down
}

restart_bridge() {
    down_bridge || true
    up_bridge
}

status_bridge() {
    compose_cmd ps
}

show_logs() {
    local lines="${2:-50}"
    compose_cmd logs --tail "$lines"
}

case "${1:-}" in
    up)
        up_bridge
        ;;
    down)
        down_bridge
        ;;
    restart)
        restart_bridge
        ;;
    status)
        status_bridge
        ;;
    logs)
        show_logs "$@"
        ;;
    config)
        config_bridge
        ;;
    preview)
        preview_bridge
        ;;
    *)
        cat <<EOF
Usage: bash scripts/manage_openwebui_bridge.sh {up|down|restart|status|logs|config|preview}

Commands:
  up       启动 Open WebUI + Pipelines 影子 bridge
  down     停止并移除 bridge 容器
  restart  重启 bridge
  status   查看 bridge 容器状态
  logs     查看 bridge 日志（可选：logs 100）
  config   展开并校验 docker compose 配置
  preview  仅打印解析后的端口与上游指向

If Open WebUI first startup becomes unhealthy while downloading its default embedding model,
seed the cache first with:
    bash scripts/seed_openwebui_embedding_cache.sh
EOF
        exit 1
        ;;
esac