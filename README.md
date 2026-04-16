# XiaMimate Open WebUI Bridge

这个仓库是 XiaMimate 拆分迁移 Phase 5 的 `openwebui bridge` 子项目骨架。

当前状态：

- 创建时间：2026-04-15
- 来源：从旧基线 `/path/to/xiamimate/open_webui` 复制最小运行集
- 当前用途：影子 bridge 骨架验证，不替换旧 Open WebUI 入口
- 默认影子端口：`13002`（Open WebUI）与 `19099`（Pipelines）

当前已迁入内容：

- `docker-compose.yml`
- `pipelines/xiamimate.py`
- `pipelines/xiamimate_mode_router.py`
- `tools/xiamimate_theme_tools.py`
- `scripts/manage_openwebui_bridge.sh`
- `scripts/dry_run_validate_openwebui_bridge.sh`

边界说明：

1. 本仓拥有 Open WebUI 与 Pipelines 的桥接运行时、slash router 与工具代理层。
2. 上游 Dify、MiniMax 与 Theme API 密钥继续由 `chat_backend` 统一承接，本仓只保留 `CHAT_BACKEND_*` 接入参数。
3. 当前 bridge 默认指向正式 `chat-backend`：`http://host.docker.internal:8200`。

推荐启动方式：

1. 复制 `.env.example` 为本地 `.env`。
2. 至少填写：
	- `OPEN_WEBUI_SECRET_KEY`
	- `PIPELINES_API_KEY`
	- `CHAT_BACKEND_SERVICE_SECRET`
3. 默认 Nginx 镜像已改为 `nginx:latest`，会优先复用本机已有镜像；如需切换，可在 `.env` 里覆盖 `OPEN_WEBUI_NGINX_IMAGE`。
4. 预览解析后的参数：
	- `bash scripts/manage_openwebui_bridge.sh preview`
5. 执行 dry-run 校验：
	- `bash scripts/dry_run_validate_openwebui_bridge.sh`
6. 如需影子启动：
	- `bash scripts/manage_openwebui_bridge.sh up`

首次启动注意：

1. 如果 `13002 /health` 长时间不变为 `200`，且日志显示默认 embedding 模型下载失败，可先执行：
	- `bash scripts/seed_openwebui_embedding_cache.sh`
2. 然后重启：
	- `bash scripts/manage_openwebui_bridge.sh restart`

当前阶段不做：

1. 不替换旧 Open WebUI 正式入口。
2. 不迁移旧仓运行时生成的数据目录。
3. 不在 phase 5 当前骨架验证中强制跑通真实用户登录与前端对话链路。