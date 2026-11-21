# 团队服务（serve.py）

GitHub Pages 是静态演示站：审核记录只存在访问者自己的浏览器里，也不能当场调用模型。团队一起审核时，请运行 serve.py：审核状态和操作记录存在服务端 SQLite，所有人看到的是同一份数据。

## 启动

1. 安装 Python 3.10+，在项目目录运行 `pip install -r requirements.txt`
2. 设置模型 Key（任选一个，见 README），例如 `set ANTHROPIC_API_KEY=...`
3. 双击 **Start.cmd**，或运行 `python serve.py`，然后打开 http://127.0.0.1:8765/
4. 在右上角填写名字，之后的每次操作都记在这个名字下

## 给同事访问

默认只监听本机。在局域网或服务器上共享时，必须同时设置团队口令：

```
set HOTSPOT_HOST=0.0.0.0
set HOTSPOT_TEAM_TOKEN=换成一段足够长的口令
python serve.py
```

同事首次打开时，在右上角输入口令。没设口令却开放给外部访问时，服务会拒绝启动。

## 采样节奏

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HOTSPOT_INTERVAL_SECONDS` | 1800 | 免费来源（微博热搜、抖音热榜、B站热门中各款游戏的条目）的采样间隔，0 表示只在启动时采一次 |
| `HOTSPOT_APIFY_INTERVAL_SECONDS` | 0 | Apify 搜索（计费）的间隔，0 表示只在点击「加跑 Apify 搜索」时运行 |
| `HOTSPOT_AI_MAX_ITEMS` | 10 | 每轮最多送 AI 分析的热点数 |
| `HOTSPOT_PORT` | 8765 | 端口 |

数据保存在 `.data/apify-hotspots.sqlite3`，包括热点、采样记录、AI 分析、审核状态和操作记录。备份这个文件即可。

## 两人同时修改

每条热点的审核状态都带版本号。你保存时如果同事刚改过同一条，系统会拒绝这次保存，载入同事的版本，并提示是谁改的。请确认后再操作。
