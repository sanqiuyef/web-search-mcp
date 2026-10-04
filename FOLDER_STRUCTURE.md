# FOLDER_STRUCTURE

web-search-server 目录结构（新增目录/文件时同步更新本文件）。

```
web-search-server/
├── server.py                 # MCP 入口：注册全部工具、线程池与超时保护、输出格式化
├── README.md                 # 项目说明与 Twitter(X) 接入配置
├── FOLDER_STRUCTURE.md       # 本文件
├── LICENSE                   # MIT
├── requirements.txt          # 依赖（含可选依赖说明）
├── .env.example              # 环境变量示例（真实值放本机 env 或 .env，不入库）
├── .gitignore                # 排除凭据、缓存与虚拟环境
├── .cache/                   # 运行时磁盘缓存（如 x_api.json：queryId/features，非凭据）
├── src/
│   ├── config.py             # 全部配置与环境变量（含 X_AUTH_TOKEN/X_CT0 读取处）
│   ├── utils.py              # URL 处理、相关性过滤、反爬检测、CDP/Playwright 渲染、结果合并
│   ├── cache.py              # 内存结果缓存（TTL）
│   ├── ratelimit.py          # 全局与逐引擎限流
│   ├── retry.py              # 重试
│   ├── crawl4ai.py           # crawl4ai 本地渲染后端
│   ├── firecrawl.py          # Firecrawl 云抓取后端
│   ├── searchengines/        # 单引擎实现：duckduckgo(ddgs)/bing/google/searxng
│   └── tools/
│       ├── advanced_search.py   # 聚合搜索（去重融合、相关性过滤、正文附取）
│       ├── basic_search.py      # 基础搜索封装
│       ├── batch_search.py      # 批量聚合
│       ├── web_fetch.py         # 抓取回退链 + 文件下载
│       ├── site_specific.py     # 零凭据站点搜索（GitHub/npm/PyPI/GitLab/crates/Maven/NuGet/Docker/HuggingFace）
│       ├── twitter.py           # Twitter(X) 站点搜索（cookie 会话 + CDP 浏览器双后端）
│       └── health.py            # 健康检查
└── tests/                    # pytest 单测（不依赖网络的 mock 测试）
```
