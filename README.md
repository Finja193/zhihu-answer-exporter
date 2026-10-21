# zhihu-answer-exporter

知乎回答批量导出工具。输入一个知乎问题链接，自动完成登录、按创建时间抓取该问题下所有指定日期之后的新回答，并导出为 JSON / TXT 文件。

提供两种使用方式：

- **Web 版**（`app.py`）：Flask + 本地网页 + SSE 实时进度，图形界面操作，适合大多数场景。
- **命令行版**（`zhihu_scraper.py`）：终端交互输入问题 ID，直接抓取并保存，适合脚本化 / 无界面环境。

## 功能特性

- Playwright 驱动 Chromium 自动化登录，验证码可人工在浏览器窗口中完成
- Cookie 缓存复用，登录态有效时不再重复拉起浏览器（通过 `/api/v4/me` 校验）
- 调用知乎 v4 开放接口分页拉取回答，`sort_by=created` 按时间排序，命中时间截止线即停止翻页
- Web 版通过 SSE（Server-Sent Events）实时推送登录 / 抓取进度到浏览器
- 结果自动落盘到 `~/Documents/ZhihuAnswers/`，文件名带问题 ID 与时间戳
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

1. 粘贴问题链接或纯数字 ID → 点击 **Start**
2. 若 Cookie 失效，会弹出 Chromium 窗口，按提示输入验证码并确认登录
3. 进度卡片实时显示登录状态、已抓取回答数与日志
4. 完成后展示总条数与保存路径

页面加载时会先调用 `/api/check_login`，若已是登录态则直接提示用户名，省去浏览器登录步骤。

### 命令行版

```bash
python zhihu_scraper.py
```

按提示输入问题 URL 或 ID，脚本依次完成：登录 → 抓取 → 保存 JSON + TXT。

### 最小化调试脚本

`test_direct.py` 是一个无 UI 的极简版本，问题 ID 写死在 `QID` 常量中，用于快速验证 Cookie 是否可用、接口是否正常：

```bash
python test_direct.py
```

## 输出格式

输出目录：`~/Documents/ZhihuAnswers/`

| 文件 | 说明 |
| --- | --- |
| `zhihu_<question_id>_<timestamp>.json` | 结构化数据（Web 版输出） |
| `zhihu_<question_id>_<timestamp>.txt` | 可读文本摘要（命令行版输出） |

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
├── app.py                    # Flask Web 服务：路由 + SSE 进度 + 后台抓取线程
├── zhihu_scraper.py          # 命令行版抓取脚本
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

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 返回前端页面 |
| `GET` | `/api/check_login` | 查询 Cookie 是否有效，返回 `{logged_in, username}` |
| `POST` | `/api/start` | 提交 `{question_url}`，创建后台任务，返回 `{task_id, question_id}` |
| `GET` | `/api/progress/<task_id>` | SSE 进度流 |

SSE 事件类型：`phase`（阶段切换）、`login_status`（登录状态）、`fetch_progress`（抓取进度）、`done`（完成）、`error`（错误）、`heartbeat`（30 秒心跳保活）。

## 打包为 exe

```bash
pyinstaller ZhihuScraper.spec
```

产物在 `dist/ZhihuScraper.exe`。`app.py` 通过 `sys._MEIPASS` 兼容 PyInstaller 的临时解压目录来定位 `templates/`。

> 当前 `.spec` 的入口是 `zhihu_scraper.py`（命令行版）。若要打包 Web 版，需将 `Analysis` 的脚本改为 `app.py`，并把 `templates` 加入 `datas`。

## 常见问题

**登录后仍反复要求登录？**
Cookie 已过期。删除 `zhihu_cookies.json` 重跑，或检查系统时间是否准确（Cookie 对时间敏感）。

**抓取中途报 `403 Forbidden`？**
登录态失效，接口拒绝访问。重新登录后再试；也可能是请求过于频繁，稍作等待。

**抓取结果为空？**
先确认问题 ID 正确、该问题确实有回答；再确认 `CUTOFF_TIMESTAMP` 没有设成未来时间。

**浏览器窗口没有弹出 / 登录页元素找不到？**
知乎登录页改版会导致定位器失效。相关选择器集中在登录代码段的中文常量（`TEXT_LOGIN`、`TEXT_PASSWORD_LOGIN`）与输入框定位处，需要按新版页面结构调整。

**Web 版任务卡在某个阶段？**
SSE 连接断开时前端会直接 `close()`，但后台线程仍在运行。可通过输出目录中是否生成文件判断任务是否已完成。