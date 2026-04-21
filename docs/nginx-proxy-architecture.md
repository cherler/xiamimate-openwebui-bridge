# XiaMimate Bridge Nginx Proxy Architecture

## 当前代理拓扑

当前 bridge 的 Nginx 只直接反向代理三类上游：

1. `openwebui`
   - upstream: `open-webui:8080`
   - 入口职责：站点根路径、Open WebUI 自身后台、静态资源、WebSocket、登录鉴权接口
   - 代表路径：`/`、`/admin`、`/api/v1/auths/`、`/_app/`、`/static/`、`/ws/`、`/manifest.json`

2. `chatbackend`
   - upstream: `host.docker.internal:8200`
   - 入口职责：Portal 页面、Portal API、Portal/Open WebUI 的会话鉴权子请求、bridge 管理后台
   - 代表路径：`/_portal_session_check`、`/_openwebui_verified_user_check`、`/portal/...`、`/admin/backoffice`、`/admin/api/`

3. `Dify chatbot`
   - upstream: `${DIFY_CHATBOT_BASE_URL}`
   - 入口职责：通过同域 `/_dify/` 暴露 Dify chatbot，统一处理静态资源、API 前缀与重定向改写
   - 代表路径：`/_dify/...`

## 不在 Nginx 里直代理的服务

`theme_api` 当前不在 bridge Nginx 中直接反代。

它现在的调用链是：

1. Open WebUI / tools / pipelines
2. `chat_backend` 内部 provider 路由
3. 再由 `chat_backend` 去访问 `theme_api`

这意味着 bridge 当前承担的是“统一入口与鉴权编排”，不是“所有内部服务的总反向代理”。

## 当前模块划分

`nginx/default.conf` 现在保留：

1. 顶层 `map` / `upstream` 定义
2. 路由编排和访问控制决策
3. 依赖环境变量的 Dify 与管理员信任头逻辑

`nginx/includes/` 现在承载共享片段：

1. `proxy_common_headers.conf`
   - 统一 `Host`、真实 IP、Forwarded 头
2. `proxy_websocket_headers.conf`
   - 统一 WebSocket 升级头
3. `proxy_accept_encoding_off.conf`
   - 统一关闭上游压缩，便于 `sub_filter`
4. `proxy_auth_request_headers.conf`
   - 统一内部鉴权子请求的无 body + Cookie 转发
5. `cache_no_store.conf`
   - 统一 no-store 响应头

## 后续接入新内部代理的建议方式

如果后续要在 bridge 里再增加一个内部服务，建议遵循下面的模式：

1. 先决定它是否真的应该由 Nginx 直代理。
   - 如果只是 Open WebUI 工具调用，优先继续走 `chat_backend` 统一 provider。
   - 如果是要给浏览器直接访问，才考虑在 bridge 增加新前缀代理。

2. 新服务一律给独立前缀，不和根路径混用。
   - 推荐形式：`/_service_name/`
   - 例如：`/_theme_api/`、`/_rag_admin/`

3. 复用共享片段，不再复制 header 模板。
   - 普通 HTTP 代理：`proxy_common_headers.conf`
   - 需要 WebSocket：再加 `proxy_websocket_headers.conf`
   - 需要 HTML 改写：再加 `proxy_accept_encoding_off.conf` 与 `sub_filter`

4. 提前定义这个新前缀的鉴权模型。
   - 匿名公开
   - Open WebUI 登录用户
   - Open WebUI 管理员
   - chat-backend 自定义验证

5. 浏览器可见的子路径代理，要同时检查三件事。
   - 静态资源路径
   - 前端内置 API 前缀
   - upstream 返回的 `Location` 重定向

## 当前可继续优化的点

1. 如果未来直代理的内部服务继续增多，可以把 route 层继续拆成多个模板文件，再配合单独的模板渲染流程输出到 Nginx include 目录。
2. `/_dify/` 现在已经具备“子路径代理适配器”特征，后续可抽象成同类服务的标准模板。
3. 如果 Theme API 未来需要浏览器直连，再单独给它一个前缀代理，不要混入 `/portal` 或 `/admin`。
