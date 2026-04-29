#!/usr/bin/env bash

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENWEBUI_BRIDGE_ENV_FILE="${XIAMIMATE_OPENWEBUI_BRIDGE_ENV_FILE:-$ROOT_DIR/.env}"

set_default_if_missing() {
    local var_name="$1"
    local candidate="$2"

    if [[ -n "${!var_name:-}" ]]; then
        return 0
    fi
    if [[ -z "$candidate" ]]; then
        return 0
    fi

    printf -v "$var_name" '%s' "$candidate"
    export "$var_name"
}

if [[ -f "$OPENWEBUI_BRIDGE_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$OPENWEBUI_BRIDGE_ENV_FILE"
    set +a
fi

if [[ -z "${XIAMIMATE_BASELINE_ROOT:-}" ]]; then
    default_baseline_root="$(cd "$ROOT_DIR/../xiamimate" 2>/dev/null && pwd || true)"
    if [[ -n "$default_baseline_root" && -d "$default_baseline_root" ]]; then
        XIAMIMATE_BASELINE_ROOT="$default_baseline_root"
    fi
fi

set_default_if_missing "OPEN_WEBUI_NGINX_IMAGE" "nginx:latest"
set_default_if_missing "OPEN_WEBUI_IMAGE" "ghcr.io/open-webui/open-webui:main"
set_default_if_missing "PIPELINES_IMAGE" "ghcr.io/open-webui/pipelines:main"
set_default_if_missing "OPEN_WEBUI_PORT" "13002"
set_default_if_missing "PIPELINES_PORT" "19099"
set_default_if_missing "OPEN_WEBUI_SECRET_KEY" "replace-with-a-long-random-string"
set_default_if_missing "PIPELINES_API_KEY" "replace-with-another-random-string"
set_default_if_missing "XIAMIMATE_MODEL_PREFIX" "xiamimate"
set_default_if_missing "CHAT_BACKEND_BASE_URL" "http://host.docker.internal:8200"
set_default_if_missing "CHAT_BACKEND_TIMEOUT" "30"
set_default_if_missing "CHAT_BACKEND_SERVICE_SECRET" "replace-with-a-long-random-string"
set_default_if_missing "CHAT_BACKEND_TRUSTED_ADMIN_SERVICE_NAME" "openwebui-bridge-admin"
set_default_if_missing "CHAT_BACKEND_SERVICE_NAME" "open-webui-pipeline"
set_default_if_missing "DIFY_CHATBOT_BASE_URL" "http://host.docker.internal:80"
set_default_if_missing "DIFY_REQUEST_TIMEOUT" "180"
set_default_if_missing "AGENT_OPENAI_MODEL" "deepseek-v4-pro"
set_default_if_missing "AGENT_ANTHROPIC_MODEL" "MiniMax-M2.7-highspeed"
set_default_if_missing "AGENT_MODEL_DEFAULT_PROFILE" "deepseek"
set_default_if_missing "AGENT_MODEL_PROFILES" "deepseek,minimax"
set_default_if_missing "AGENT_MODEL_DEEPSEEK_LABEL" "DeepSeek V4 Pro"
set_default_if_missing "AGENT_MODEL_MINIMAX_LABEL" "MiniMax M2.7"

export XIAMIMATE_BASELINE_ROOT