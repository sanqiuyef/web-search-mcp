# web-search-server

面向 AI 的本地 Web 搜索 MCP 服务：多引擎聚合搜索、网页抓取回退链、站点定向搜索。

- 传输：stdio（在 MCP 客户端里注册本地 MCP）；默认端口 8011（HTTP 模式时）
- 运行：`python server.py`（依赖见 `requirements.txt`；可选依赖缺失会自动降级）
- 测试：`python -m pytest tests/ -q`（全部为 mock 测试，不依赖网络）

## 工具清单（18）

| 类别 | 工具 |
|---|---|
| 单引擎搜索 | `web_search`（DuckDuckGo/ddgs）、`web_search_bing`、`web_search_google`（需代理） |
| 聚合搜索 | `web_search_advanced`、`web_search_with_content`（附正文）、`batch_web_search` |
| 网页抓取 | `web_fetch`（反爬→crawl4ai→firecrawl→CDP→Playwright 回退链）、`download_file` |
| 站点搜索·零凭据 | `github_repo_search`、`npm_package_search`、`pypi_package_search`、`gitlab_repo_search`、`crates_package_search`、`maven_package_search`、`nuget_package_search`、`docker_image_search`、`huggingface_model_search` |
| 站点搜索·需登录态 | `twitter_search`（Twitter/X 推文搜索） |

## 站点定向搜索

直接调各平台 API / 复用浏览器会话取结构化结果，比通用网页搜索准确（GitHub 走公开
REST API，无需任何凭据）。

## Twitter(X) 接入（twitter_search）

X 没有免费公开搜索 API，未登录访问 x.com 是空白页（2026-10-04 实测）；
公共 Nitter 镜像当天也全部不可用（Cloudflare / Anubis / 429）。所以读 X 必须借登录态，
三条通路按推荐顺序：

### ① 遇到反爬/登录墙，回退用 Playwright MCP 驱动已登录的浏览器（零配置，推荐）

本项目的回退原则：本地抓取/渲染过不去（反爬、登录墙）时，**最终回退是交给
Playwright MCP（`mcp__playwright__*`，browser_* 工具）在用户已登录的浏览器里打开页面完成任务**
—— 不自己造浏览器、不要密码。做法：让 Playwright MCP 以
`--browser msedge --executable-path <系统 Edge> --extension` 的扩展模式桥接你自己的浏览器，
X 登录态就在里面（2026-10-04 实测：x.com 搜索页可直接抽到实时推文）。读 X 时：
`browser_tabs(action="new", url=...)` 开新标签页（别动扩展 connect 页），再用
`browser_evaluate` 抽取 `article[data-testid="tweet"]`。

### ② cookie 会话（静默，本工具自带）

不需弹浏览器时用这条路：

1. 在已登录 x.com 的浏览器（如 Edge）里：F12 → Application → Cookies → `https://x.com`
2. 复制 `auth_token` 与 `ct0` 两个值
3. 配到 MCP 客户端的 env（如 ZCode：`%USERPROFILE%\.zcode\cli\config.json` →
   `mcp.servers.web-search-server.env`）：

```json
"env": {
  "X_AUTH_TOKEN": "<auth_token 的值>",
  "X_CT0": "<ct0 的值>"
}
```

4. 重启 MCP 客户端后生效。cookie 等同于账号凭据：只放本机 env，不要写进仓库、
   笔记、聊天记录或记忆文件（本仓库代码也不会打印/落盘它们）。
   - 登录态过期时工具会提示「cookie 无效或已过期」，重新复制即可。

### ③ 调试端口浏览器（进阶，本工具自带）

Edge/Chrome **≥136 禁止用默认配置目录开 `--remote-debugging-port`**（实测 Edge 154
拒绝），且实测把 Edge 默认配置复制到独立目录启动后 **X 仍是未登录**。所以这条路需要：

```powershell
# 用独立配置目录启动（首次在该窗口里登录一次 X，之后登录态常驻）
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  --user-data-dir="<独立目录>\edge-agent-profile" --remote-debugging-port=9222
```

保持该实例运行，本工具的浏览器后端会新开标签页搜索、抽取后关闭（用户可见）。
Chrome 以调试端口启动期间不要用它登录敏感账号（调试端口对本机程序开放）。

### 用法

```
twitter_search("audio llm")                 # 最新推文
twitter_search("audio llm", mode="top")     # 热门
twitter_search("from:elonmusk grok")        # 读某人推文（X 搜索语法通用）
twitter_search("@elonmusk grok")            # 提及
```

返回：@handle、时间、正文、互动数（回复/转推/喜欢/查看）、推文链接。

### 实现说明（改版自愈）

| 环节 | 做法 |
|---|---|
| 接口地址 | `https://x.com/i/api/graphql/{queryId}/SearchTimeline` |
| queryId | 自动从 `abs.twimg.com` 前端 bundle 里发现，结果缓存 24h（`.cache/x_api.json`）；失败可手工设 `X_SEARCH_QUERY_ID` |
| features | X 报 `features cannot be null: ...` 时自动补齐同名 feature 重试并缓存 |
| 401/403 | 提示重新复制 cookie；404 自动重发现 queryId 重试 |
| 兜底 | 没有可用登录态时返回配置指引（不返回空结果/假数据） |

## 主要环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `WS_SEARXNG_URL` | 空 | 自托管 SearXNG 实例（WSL2 部署于 127.0.0.1:8080） |
| `WS_DDGS_BACKENDS` | `yandex` | ddgs 固定后端（本机实测仅 yandex 可用） |
| `WS_RELEVANCE_FILTER` | `true` | 查询词相关性过滤 |
| `WS_CACHE_TTL` | `300` | 搜索结果内存缓存秒数 |
| `X_AUTH_TOKEN` / `X_CT0` | 空 | X 的 cookie 会话（见上） |
| `X_SEARCH_QUERY_ID` | 空 | 手工覆盖 SearchTimeline queryId |
| `WS_CDP_PORT` | `9222` | CDP 复用的浏览器调试端口（抓取回退链与 twitter_search 共用） |
| `FIRECRAWL_API_KEY` | 空 | 可选云抓取后端 |
