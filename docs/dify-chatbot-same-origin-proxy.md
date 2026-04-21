# Dify Chatbot 同域代理部署说明

这份说明用于把 Dify chatbot 稳定挂到 bridge 的同域 `/_dify/` 前缀下，避免浏览器直连 `localhost`、SSH 隧道地址或内网地址，并减少后续线上回归。

## 目标

目标是同时满足下面几件事：

1. 浏览器只访问当前站点同域地址，例如 `https://your-domain/_dify/...`
2. Portal / Guide 页面里的 bubble 点击后可以正常打开 Dify 聊天窗口
3. 首次打开可能略慢，但不能永久停在 loading
4. 后续切页或再次点击时可以利用浏览器缓存，明显更快
5. 线上部署时不依赖开发机 SSH 隧道或 `127.0.0.1`

## 环境变量

bridge 侧至少需要这项：

```dotenv
DIFY_CHATBOT_BASE_URL=http://host.docker.internal:18880
```

说明：

1. 本地调试时，它可以指向宿主机端口、反代后的 Dify 地址，或临时 SSH 隧道入口。
2. 面向用户部署时，它必须改成 bridge 所在主机或容器网络里可以直连的 Dify 地址，不能要求终端用户浏览器去访问 `localhost`。
3. 这个值只应该被 Nginx bridge 使用；浏览器最终看到的始终是同域 `/_dify/`。

## Nginx 必备规则

核心文件是 [nginx/default.conf](../nginx/default.conf)。

### 1. `/_dify/` 主入口

主入口负责三类事情：

1. 反代 Dify chatbot 页面与 API
2. 把 upstream 返回的重定向改写成 `/_dify/...`
3. 改写 HTML 和 SSR hydration 里仍指向根路径的前缀

必须保留的改写点：

1. `href="/_next/`、`src="/_next/`、`"/_next/` 改到 `/_dify/_next/`
2. `data-base-path=""` 改到 `data-base-path="/_dify"`
3. `data-api-prefix="/console/api"` 改到 `data-api-prefix="/_dify/console/api"`
4. `data-public-api-prefix="/api"` 改到 `data-public-api-prefix="/_dify/api"`
5. SSR hydration JSON 中的 `\"data-base-path\":\"\"`、`\"data-api-prefix\":\"/console/api\"`、`\"data-public-api-prefix\":\"/api\"` 也必须同步改写

原因：React/Next 客户端真正消费的是 SSR JSON 那一份，不只看 DOM 属性。

### 2. `/_dify/api/webapp/permission` 桩返回

自托管 Dify 在没有 EnterpriseService 时，这个接口可能返回 500，即使 access mode 是 public。前端会卡在它的 promise 上，表现就是 bubble 打开后一直 loading。

bridge 需要在 `^~ /_dify/` 之前放一条精确匹配：

```nginx
location = /_dify/api/webapp/permission {
    default_type application/json;
    add_header Cache-Control "no-store" always;
    return 200 '{"result":true}';
}
```

### 3. `/_dify/_next/static/` 大资源直通

不要对整段 `/_dify/_next/static/` 统一做 `sub_filter application/javascript`。

原因：

1. Dify 的 Next/Turbopack chunk 很多，而且部分 chunk 很大
2. 如果所有 JS 都边转发边改写，浏览器会出现大量脚本长时间 pending
3. 页面会长期停在 `document.readyState = interactive`，看起来像白屏加载失败

正确做法：

1. `/_dify/_next/static/` 大部分资源直通 upstream
2. 只对 `turbopack-*.js` 单独关闭压缩并改写 `let t="/_next/" -> let t="/_dify/_next/"`

也就是：

1. HTML/SSR 负责改写入口引用和 API 前缀
2. Turbopack runtime 只负责修正客户端动态 chunk publicPath
3. 其他大 chunk 保持原样透传

## Portal 注入要求

Portal 侧注入代码当前在 [data_platform/api/chat_backend_portal_public_html.py](../../xiamimate-chat-backend/data_platform/api/chat_backend_portal_public_html.py) 里。

必须满足：

1. `baseUrl` 指向同域 `/_dify`
2. 在加载 `embed.min.js` 之前，先确保 `localStorage` 里有 `passport-${token}`；只有本地没有 passport、或已有 passport 明显失效时，才请求 `/_dify/api/passport` 并写入新值，不能每个 portal 页面都强制覆盖
3. `dynamicScript: true`，因为 portal 是动态注入脚本，不走 Dify 默认的 `body.onload` 时机

补充说明：

1. 自托管 Dify 的 `/_dify/api/passport` 每次请求都可能返回新的 `end_user_id`
2. 如果每次切页都覆写 `passport-${token}`，而浏览器本地还保留上一个 conversation id，就会在新页面恢复会话时命中 `/_dify/api/messages?conversation_id=...`，随后报 `Conversation Not Exists`
3. portal 注入层应该优先复用现有 passport，并在加载 embed 前校验/清理失效的 `conversationIdInfo`

## 首次打开慢的原因

首次点击 bubble 比后续慢，通常是正常现象，主要来自这几段冷启动成本叠加：

1. 首次打开 iframe 时，浏览器第一次下载 Dify chatbot HTML
2. HTML 再触发首批 Next/Turbopack 静态资源下载
3. Dify 客户端随后发起 `setup`、`system-features`、`access-mode`、`passport`、`site`、`meta` 等初始化 API
4. 如果资源和 API 还没进浏览器缓存，用户会感受到明显等待

后续切页再打开变快，是因为：

1. 浏览器已经缓存了大部分 `/_dify/_next/static/...` 资源
2. 同一 share code 对应的初始化请求也大多已经走过
3. 本地 `localStorage` 里已有 `passport-${token}`

## 可选优化：预热但不提前弹窗

portal 可以做轻量预热，而不是直接预加载一个隐藏聊天 iframe。

建议顺序：

1. 页面初始化时先完成 `passport` bootstrap
2. 等浏览器空闲时，低优先级抓取一次 `/_dify/chatbot/<share-code>` HTML
3. 从 HTML 中抽出首批 `/_dify/_next/static/...` 的 JS/CSS，追加 `prefetch` hint
4. 遇到 `saveData` 或 2G 网络时自动跳过，避免给弱网用户增加负担

这样做的好处：

1. 不会提前弹出聊天窗口
2. 不会在页面上产生第二个隐藏 iframe
3. 能明显降低第一次点击 bubble 的等待感

## 上线回归检查清单

每次改 bridge、Dify 版本或 portal 注入后，至少检查下面几项：

1. 打开 `/_dify/chatbot/<share-code>`，确认 10 秒内进入 `complete`
2. 页面标题恢复为实际 Dify 应用标题，而不是空标题
3. 页面里出现输入框或发送按钮，而不是只有 spinner
4. 网络里能看到这些初始化请求走在 `/_dify/...` 下：
   - `/_dify/console/api/setup`
   - `/_dify/console/api/system-features`
   - `/_dify/api/webapp/access-mode`
   - `/_dify/api/login/status`
   - `/_dify/api/passport`
   - `/_dify/api/parameters`
   - `/_dify/api/site`
   - `/_dify/api/meta`
5. `/_dify/api/webapp/permission` 返回 `200 {"result":true}`
6. 从 `3002 -> /portal/guide -> 点击 bubble` 的真实用户路径验证，确认不是只在直开 `/_dify/chatbot/...` 时正常

## 常见回归点

下面几个问题最容易让线上再次回到“点击 bubble 一直 loading”：

1. 把浏览器端 `baseUrl` 改回 Dify 原始地址，导致用户浏览器直接访问 `localhost` 或内网地址
2. 只改写了 HTML 属性里的 API 前缀，没有改写 SSR hydration JSON
3. 删除了 `/_dify/api/webapp/permission` 桩返回
4. 重新对整个 `/_dify/_next/static/` 启用统一 JS `sub_filter`
5. Turbopack runtime 里的 `let t="/_next/"` 没被改到 `/_dify/_next/`
6. Portal 注入里没先写 passport，或者 `dynamicScript` 丢失

## 最小人工验收路径

推荐每次都按这条路径复验：

1. 打开 `http://127.0.0.1:3002/portal/guide`
2. 观察右下角 bubble 是否出现
3. 第一次点击 bubble，等待聊天窗口内容加载
4. 输入一条测试消息，确认发送与返回都正常
5. 切换页面后再回来点击一次，确认二次打开明显更快

如果这条链路通过，说明同域代理、portal 注入、静态资源路径、初始化 API 和 Dify 前端 hydration 基本都在正常状态。
