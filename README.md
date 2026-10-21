# zhihu-answer-exporter

知乎内容批量导出工具，包含两个抓取模式：

- **按问题**（`zhihu_scraper.py` / Web 的 `By Question`）：输入问题链接，按创建时间抓取该问题下指定日期之后的新回答，导出 JSON / TXT。
- **按话题**（`topic_scraper.py` / Web 的 `By Topic (CSV)`）：抓取话题「精选」回答（如 `https://www.zhihu.com/topic/19553148/top-answers`），导出 CSV / UTF-8，默认 2000 条，并输出字段缺失率报告。

使用方式：

- **Web 版**（`app.py`）：Flask + 本地网页 + SSE 实时进度，图形界面操作，适合大多数场景。
- **命令行版**：终端交互输入问题 ID / 话题 ID，直接抓取并保存，适合脚本化 / 无界面环境。

## 功能特性

- Playwright 驱动 Chromium 自动化登录，验证码可人工在浏览器窗口中完成
- Cookie 缓存复用，登录态有效时不再重复拉起浏览器（通过 `/api/v4/me` 校验）
- 调用知乎 v4 开放接口分页拉取回答，`sort_by=created` 按时间排序，命中时间截止线即停止翻页
- Web 版通过 SSE（Server-Sent Events）实时推送登录 / 抓取进度到浏览器
- 话题模式：12 个目标字段 + 答案链接，UTF-8(BOM) CSV，附带字段缺失率报告
- 结果自动落盘到 `~/Documents/ZhihuAnswers/`，文件名带 ID 与时间戳
- 支持 PyInstaller 打包为单文件 exe 发行

## 环境要求

- Python 3.8+
- 依赖：`flask`、`requests`、`playwright`

## 安装

```bash
# 1. 安装 Python 依赖
pip install flask requests playwright

# 2. 安装 Chromium 浏览器内核（首次必须）
playwright install chromium
```

## 配置

编辑 `app.py` 或 `zhihu_scraper.py` 顶部的配置区：

```python
PHONE = "你的手机号"                                  # 登录账号
PASSWORD = "你的密码"                                 # 登录密码
COOKIES_FILE = "zhihu_cookies.json"                   # Cookie 缓存文件
OUTPUT_DIR = str(Path.home() / "Documents" / "ZhihuAnswers")  # 输出目录
CUTOFF_TIMESTAMP = int(datetime(2024, 1, 1).timestamp())     # 只导出该日期之后的回答
```

两个文件的配置需保持一致（`app.py` 与 `zhihu_scraper.py` 目前各自独立定义）。

> ⚠️ **安全提醒**
> - 源码中目前直接写入了明文手机号和密码，请务必修改为你自己的账号，且不要把填好凭据的代码推到公开仓库。
> - `zhihu_cookies.json` 相当于登录凭证，任何拿到该文件的人都可以复用你的知乎身份。请勿提交到 Git（建议加入 `.gitignore`），也勿外发。
> - 抓取内容仅供个人学习研究使用，请遵守知乎 robots 协议与相关法规，控制请求频率（代码中已内置每页 1 秒间隔）。

## 使用

### Web 版

```bash
python app.py
```

启动后自动打开 <http://127.0.0.1:5000>，页面流程：

1. 顶部选择模式：**By Question**（按问题）或 **By Topic (CSV)**（按话题）
2. 粘贴链接或纯数字 ID → 点击 **Start**（话题模式还可设置抓取条数，默认 2000）
3. 若 Cookie 失效，会弹出 Chromium 窗口，按提示输入验证码并确认登录
4. 进度卡片实时显示登录状态、已抓取条数、进度条与日志；话题模式完成后额外展示核心字段缺失率
5. 完成后展示总条数与保存路径

页面加载时会先调用 `/api/check_login`，若已是登录态则直接提示用户名，省去浏览器登录步骤。

### 命令行版

```bash
python zhihu_scraper.py
```

按提示输入问题 URL 或 ID，脚本依次完成：登录 → 抓取 → 保存 JSON + TXT。

### 话题模式（命令行）

```bash
# 抓取中医话题（19553148）精选回答 2000 条
python topic_scraper.py --topic 19553148 --target 2000

# 指定输出目录与请求间隔
python topic_scraper.py --topic 19553148 --target 2000 --out ./output \
                        --delay-feed 0.4 --delay-detail 0.25
```

参数说明：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--topic` | `19553148` | 话题 ID，也支持直接传话题页 URL |
| `--target` | `2000` | 抓取条数 |
| `--out` | `~/Documents/ZhihuAnswers` | 输出目录 |
| `--delay-feed` | `0.4` | 翻页间隔（秒） |
| `--delay-detail` | `0.25` | 详情请求间隔（秒） |
| `--no-autosave` | 关闭 | 默认每 200 条断点落盘一次 |
| `--no-expand` | 关闭 | 只抓主话题；默认自动合并子话题以突破 650 条上限 |
| `--keyword` | 主话题名 | 相关性过滤关键词，只保留话题标签含该词的回答 |

### 最小化调试脚本

`test_direct.py` 是一个无 UI 的极简版本，问题 ID 写死在 `QID` 常量中，用于快速验证 Cookie 是否可用、接口是否正常：

```bash
python test_direct.py
```

## 话题模式说明

### 抓取内容

话题「精选」回答流，对应页面 `https://www.zhihu.com/topic/{topic_id}/top-answers`。

### 导出字段（CSV / UTF-8）

| 列 | 来源 | 说明 |
| --- | --- | --- |
| 博主名称 | 问题详情 `author.name` | 提问者昵称 |
| 博主认证类型 | `author.badge_v2` / `badge` | 如 `已认证·医师执业证书持证人`、`个人·未认证`、`机构·未认证` |
| 答主名称 | 回答 `author.name` | 回答者昵称 |
| 答主认证类型 | 同上 | 同上 |
| 问题标题 | 回答 `question.title` | |
| 发布时间 | 回答 `created_time` | `YYYY-MM-DD HH:MM:SS` |
| 正文 | 回答 `content` | 已去除 HTML 标签、解码实体、压缩空白 |
| 话题标签 | 问题详情 `topics[].name` | `\|` 分隔，如 `中医\|中医学\|中药` |
| 赞同数 | 回答 `voteup_count` | |
| 评论数 | 回答 `comment_count` | |
| 收藏数 | 回答 `favlists_count` | |
| 点赞数 | 回答详情 `thanks_count` | 知乎回答侧的「感谢 / 喜欢」数；赞同数已单独一列 |
| 答案链接 | 拼接 | 便于回溯与去重 |

文件编码为 `utf-8-sig`（UTF-8 带 BOM），Excel 双击打开不乱码。

### 缺失率保障

每条记录抓取后统计各字段缺失数与缺失率，写入 `zhihu_topic_<topic_id>_<timestamp>_report.json` 并在终端 / Web 结果卡片展示。核心字段定义为：博主名称、答主名称、问题标题、发布时间、正文，默认要求最大缺失率 ≤ 5%。

保证手段：

- 问题详情按 `question_id` 缓存，同一问题下的多个答案只请求一次
- 请求失败自动重试 2 次（含退避），签名失效时自动重新截取
- 每 200 条断点落盘，中断也不丢已抓数据
- 候选量按 `target × 1.8` 预收集，过滤后不足也不会半途而废

### 实测结果（中医话题 19553148，2026-10）

| 指标 | 单话题 | 主话题 + 子话题合并 |
| --- | --- | --- |
| 抓到条数 | 625 | **2000** |
| 覆盖问题数 / 答主数 | 254 / 523 | 638 / 1466 |
| 核心字段最大缺失率 | 0.32% | **0.20%**（仅 4 条无正文） |
| 数值字段 | 非数字 0、负数 0 | 非数字 0、负数 0 |
| 重复答案 | 0 | 0 |
| 话题标签含「中医」 | 625 / 625 | 2000 / 2000 |

各话题去新增量（候选 3829 条，取前 2000 条补齐）：中医 625、中医考研 150、中医内科 529、现代中医 709、中医外科 496、脊髓损伤康复 372、黄帝内经 456、中医客观化 492。

注意：越往后的子话题长尾内容越多，赞同数中位数会从主话题的约 3800 降到全量约 100，属正常现象。

**为什么单话题只有约 650 条（不是反爬）**：feed 接口存在**分页深度上限**，实测在 `offset≈1000~1200` 之后不再返回数据（返回 `{"paging":null,"data":null}`，HTTP 仍为 200、无 `error` 字段）。该上限与话题内容量无关——中医与其子话题（中医外科等小话题）都在 `offset 2000` 归零，说明是服务端限制深翻，而非「精选内容只有这么多」。话题下的其它流（最新讨论等）同样在 `offset 3000` 前耗尽。

**解决方案：合并子话题**。默认（不加 `--no-expand`）会自动抓主话题 + 全部子话题的精选流，按答案 ID 去重。实测中医话题的 10 个子话题（中医内科、现代中医、中医外科、黄帝内经…）与主话题重叠率很低，前 5 个就贡献 2323 条去重新增，因此 `--target 2000` 可以达成。

为保证主题相关性，默认用主话题名（如「中医」）过滤：只保留**话题标签含该关键词**的回答（子话题中偏离主题的内容会被丢弃，例如「脊髓损伤康复」里不含中医标签的回答）。过滤在取问题详情后、取答案详情前执行，可省掉无效请求。报告 JSON 的 `各话题贡献` 记录了每个话题的去新增量。

判定依据（可与真实反爬区分）：

| 现象 | 当前情况（深度上限） | 真实反爬 / 签名失效 |
| --- | --- | --- |
| HTTP 状态 | `200` | `403` |
| 响应体 | `{"paging":null,"data":null}` | `{"error":{"code":10003,...}}` |
| 响应头 | 无 `Retry-After` | 可能带限流头 |
| 与 offset 的关系 | 小 offset 同时仍可取到数据 | 与 offset 无关，整体失败 |

关键对照实验：同一 session、毫秒级间隔交替请求 `offset=200`（13 条）与 `offset=1500`（0 条）各两次，结果稳定复现。限流作用于 IP / 账号，不会「只封大 offset」。

若确实需要 2000 条以上，需绕开 feed 分页：按话题下的问题列表逐个抓回答（不受该深度限制），或合并多个子话题的结果。

**正文的已知边界**：正文存纯文本，`<img>` 图片会被剥离，视频只保留链接；纯视频 / 纯图片回答的正文为空（本次 625 条中有 2 条，已回读接口确认其 `content` 本身就是空串，非漏抓）。

### 实现要点（签名）

话题 feed 走的是 `/api/v5.1/topics/{id}/feeds/top_activity/v2`，带 `x-zse-96` 签名校验，`requests` 直接请求会返回 `403 / code 10003`。因此：

1. Playwright（无头）打开一次话题页，从真实请求中截取 `x-zse-96`、`x-zse-93`、`User-Agent`
2. 实测该签名可跨 URL 复用 —— 后续 feed 翻页与问题详情请求全部由 `requests` 携带该签名发起
3. 若中途出现 403，自动重新打开页面截取新签名并重试

这样既绕开了 JS 签名算法，又避免了「滚动页面 100 次」的慢速抓取。

## 输出格式

输出目录：`~/Documents/ZhihuAnswers/`

| 文件 | 说明 |
| --- | --- |
| `zhihu_<question_id>_<timestamp>.json` | 按问题模式的结构化数据（Web 版输出） |
| `zhihu_<question_id>_<timestamp>.txt` | 按问题模式的可读文本摘要（命令行版输出） |
| `zhihu_topic_<topic_id>_<timestamp>.csv` | 话题模式导出的 CSV（UTF-8 BOM） |
| `zhihu_topic_<topic_id>_<timestamp>_report.json` | 话题模式字段缺失率报告 |

单条记录的字段：

| 字段 | 含义 |
| --- | --- |
| `id` | 回答 ID |
| `author` | 作者昵称 |
| `author_url` | 作者主页地址（命令行版） |
| `content` | 正文，已去 HTML 标签并解码实体、压缩空白 |
| `created_time` | Unix 时间戳（命令行版） |
| `created_date` | 创建时间 `YYYY-MM-DD HH:MM:SS` |
| `voteup_count` | 赞同数 |
| `comment_count` | 评论数 |
| `url` | 回答完整链接 |

## 项目结构

```
.
├── app.py                    # Flask Web 服务：路由 + SSE 进度 + 后台抓取线程（两种模式）
├── zhihu_scraper.py          # 按问题抓取（命令行版）
├── topic_scraper.py          # 按话题抓取精选回答并导出 CSV（命令行版 + Web 复用）
│                             #   scrape_topic_answers: 单话题
│                             #   scrape_topic_tree: 主话题 + 子话题合并（突破深度上限）
├── test_direct.py            # 最小化调试脚本（硬编码问题 ID）
├── templates/
│   └── index.html            # 单页前端（原生 HTML/CSS/JS，无构建步骤）
├── ZhihuScraper.spec         # PyInstaller 打包配置
├── zhihu_cookies.json        # 登录 Cookie 缓存（敏感，勿提交）
├── build/                    # PyInstaller 中间产物
├── dist/                     # PyInstaller 输出（exe）
└── output/                   # 本地临时输出
```

## 工作原理

1. **登录**：Playwright 启动可见的 Chromium，打开 `https://www.zhihu.com/signin`，切换到密码登录、填入账号密码并提交；等待跳转，超时则给 30 秒人工确认窗口；成功后把浏览器 Cookie 序列化写入 `zhihu_cookies.json`。
2. **校验**：下次运行时先读 Cookie，请求 `/api/v4/me`，返回中含 `id` 即视为有效，跳过浏览器登录。
3. **抓取**：以 `limit=20&offset=N` 翻页请求 `/api/v4/questions/{qid}/answers`，携带 `include=data[*].content,excerpt` 直接取正文。逐条比对 `created_time` 与 `CUTOFF_TIMESTAMP`，一旦早于截止线即停止（依赖按创建时间排序的前提）。
4. **清洗**：`strip_html()` 去标签 → `unescape()` 解实体 → 合并空白，得到纯文本正文。
5. **保存**：写入带时间戳的 JSON（以及命令行版的 TXT），通过 SSE 推送 `done` 事件通知前端。

话题模式的流程：

1. **取签名**：Playwright 无头打开话题页，从 `top_activity/v2` 请求头截取 `x-zse-96`
2. **翻页**：`requests` 携带签名请求 `/api/v5.1/topics/{tid}/feeds/top_activity/v2?offset=N&limit=20&`，只取 `target.type == "answer"` 的条目，按回答 ID 去重
3. **补齐**：每条回答再取问题详情（博主 + 话题标签，按问题 ID 缓存）和回答详情（点赞数）
4. **落盘**：写 CSV + 缺失率报告，每 200 条断点保存一次

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 返回前端页面 |
| `GET` | `/api/check_login` | 查询 Cookie 是否有效，返回 `{logged_in, username}` |
| `POST` | `/api/start` | 提交 `{mode, question_url, target}`，创建后台任务 |
| `GET` | `/api/progress/<task_id>` | SSE 进度流 |

`POST /api/start` 的请求体：

```jsonc
// 按问题（默认）
{ "mode": "question", "question_url": "https://www.zhihu.com/question/12345678" }

// 按话题
{ "mode": "topic", "question_url": "19553148", "target": 2000 }
```

响应：话题模式返回 `{task_id, topic_id, target, mode}`，问题模式返回 `{task_id, question_id, mode}`。

SSE 事件类型：`phase`（阶段切换）、`login_status`（登录状态）、`fetch_progress`（抓取进度，含 `current`/`total`）、`log`（文本日志，话题模式逐条推送）、`done`（完成，含 `core_missing_rate`）、`error`（错误）、`heartbeat`（30 秒心跳保活）。

## 打包为 exe

```bash
pyinstaller ZhihuScraper.spec
```

产物在 `dist/ZhihuScraper.exe`。`app.py` 通过 `sys._MEIPASS` 兼容 PyInstaller 的临时解压目录来定位 `templates/`。

> 当前 `.spec` 的入口是 `zhihu_scraper.py`（命令行版）。若要打包 Web 版，需将 `Analysis` 的脚本改为 `app.py`，并把 `templates` 加入 `datas`。

## 常见问题

**登录后仍反复要求登录？**
Cookie 已过期。删除 `zhihu_cookies.json` 重跑，或检查系统时间是否准确（Cookie 对时间敏感）。

**抓取中途报 `403 Forbidden` / `code 10003`？**
登录态失效或接口签名失效，接口拒绝访问。话题模式会自动重新截取签名重试；若持续失败，删除 `zhihu_cookies.json` 重新登录。也可能是请求过于频繁，可调大 `--delay-feed` / `--delay-detail`。

**Web 版话题模式进度不动 / 日志停在“检查登录”？**
首次需要无头浏览器截取签名，通常 5–15 秒。若超过 90 秒仍无进展，说明话题页未能加载（Cookie 失效或网络问题），会抛错到日志区。

**CSV 里「点赞数」是什么？**
知乎回答没有独立于「赞同」的点赞按钮，该列取的是回答的感谢 / 喜欢数（`thanks_count`）。如需严格口径，可修改 `topic_scraper.py` 中 `ANSWER_API` 的 `include` 参数。

**抓取结果为空？**
先确认问题 ID 正确、该问题确实有回答；再确认 `CUTOFF_TIMESTAMP` 没有设成未来时间。

**浏览器窗口没有弹出 / 登录页元素找不到？**
知乎登录页改版会导致定位器失效。相关选择器集中在登录代码段的中文常量（`TEXT_LOGIN`、`TEXT_PASSWORD_LOGIN`）与输入框定位处，需要按新版页面结构调整。

**Web 版任务卡在某个阶段？**
SSE 连接断开时前端会直接 `close()`，但后台线程仍在运行。可通过输出目录中是否生成文件判断任务是否已完成。