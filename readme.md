# Legal Notice Batch Generator

**HTML 模板工作台** → **防提取 PDF**（整页光栅化 + AES-256 权限锁）。两种生成模式：**Excel 批量**（为 5000+ 量级设计）和 **Manual 单条**（单客户定制）。防伪细节另见 [`ANTI_COUNTERFEIT.md`](ANTI_COUNTERFEIT.md)。

## 一眼概览

登录后的 `/` 是两栏工作台——左 420 px 折叠侧栏，右全高 iframe 实时预览：

```
┌─────────────────────┬──────────────────────────────────┐
│ 1. Template         │                                  │
│ 2. Body Content (8) │                                  │
│ 3. Images (+rot)    │   LIVE iframe preview (srcdoc)   │
│ 4. Watermark        │   300 ms 防抖,边改边刷             │
│ 5. Generate         │   默认显示模板字面量占位符          │
│    · Excel batch    │                                  │
│    · Manual single  │                                  │
└─────────────────────┴──────────────────────────────────┘
```

## 系统工作原理

### 渲染流水线（每张 PDF）

```
Template blocks  ─┐
(8 data-purpose)  │
                  ├──► _swap_block_inner ──► build_notice_html ──►
Asset CSS vars   ─┤    (填充 {{占位符}})       (完整自包含 HTML)
(size/dx/dy/rot)  │                                        │
                  │                                        ▼
Security PNG    ──┘                       ┌──────────────────┐
(watermark +                              │  Playwright +     │
 guilloche +                              │  Chromium (file://)
 QR + serial)                             └─────────┬─────────┘
                                                    │  Vector PDF
                                                    ▼
                                          ┌──────────────────┐
                                          │   PyMuPDF 300dpi  │  ← 所有文字变像素
                                          │   光栅化 (零文字)  │
                                          └─────────┬─────────┘
                                                    │  Image-only PDF
                                                    ▼
                                          ┌──────────────────┐
                                          │   pikepdf AES-256 │  ← 不可 copy/edit
                                          │   Permissions lock│     可 print
                                          └─────────┬─────────┘
                                                    │
                                                    ▼
                                              Final locked PDF
```

详细每一层的防伪目标和实现见 [`ANTI_COUNTERFEIT.md`](ANTI_COUNTERFEIT.md)。

### 两种生成模式

| | Excel batch | Manual single |
|---|---|---|
| 输入 | `.xlsx` + 每行的占位符值 | 每个占位符一个 input |
| 数量 | 上千到万条 | 一条 |
| 并发 | ProcessPoolExecutor + 多 Chromium worker | 单 Chromium 即时渲染 |
| 输出 | 每组 zip，流式下载 | 单 PDF 附件直接下载 |
| 接口 | `/upload → /generate → /status → /download` | `/generate_one` |
| 用例 | 5000 条还款函批量发 | 给个别客户定制一封 |

两种模式用**同一条**渲染流水线（`render_notice_row_pdf`），区别只在编排层。

### 实时预览机制

| 场景 | 接口 | 填充策略 |
|---|---|---|
| 侧栏改模板（未保存） | `POST /api/preview_html` → `iframe.srcdoc` | 不填充（`{{foo}}` 字面量） |
| Manual 模式边填边预览 | 同上，body 带 `fields` | 部分填充（填的替换，未填保留） |
| Excel 生成的 PDF | `render_notice_row_pdf` | 填 + 剥未填的 |

一个 `build_notice_html(fill_placeholders, strip_unfilled)` 双开关控制三种模式；前端 300 ms 防抖 + `iframe.srcdoc` 避开 HTTP 往返。

## 技术栈

| 层 | 工具 | 用处 |
|---|---|---|
| Web 框架 | **Flask 3.x** | 单文件后端（~3000 行），所有 HTML 内联 |
| HTML 渲染 | **Playwright + Chromium** | HTML → 矢量 PDF（最忠实的排版） |
| PDF 光栅化 | **PyMuPDF (pymupdf)** | 整页 300 dpi 转像素，抹掉文字对象 |
| PDF 加密 | **pikepdf** | AES-256 权限锁 + 元数据 |
| 水印合成 | **Pillow** | 双语水印 + guilloche 底纹 → PNG |
| 乌尔都文字整形 | **arabic_reshaper + python-bidi** | 阿拉伯字母连写 + 双向文本 |
| QR 码 | **qrcode** | 英文水印文本 → QR PNG |
| Excel 读取 | **openpyxl** | 列名匹配占位符 |
| 并发 | `concurrent.futures.ProcessPoolExecutor` | 每 worker 独立 Chromium |
| 异步任务 | 进程内 `TASKS` dict + 线程 | ⚠ gunicorn 必须 `--workers 1 --threads N` |
| 前端 | Vanilla JS + CSS Grid | iframe `srcdoc` 实时预览，`<details>` 折叠 |
| 字体 | Libre Baskerville / Playfair Display / EB Garamond / Arial Unicode | 随包，不走 CDN |
| 部署 | Gunicorn + Nginx + systemd | `deploy/setup.sh` 一键 |

## 核心特性

### 模板层
- **多模板**：内置 `default`（**禁止删除**，按钮自动隐藏），可"另存为新模板"任意克隆、改名、删除
- **7 块可编辑正文**：letterhead-firm / letterhead-partners / notice-subject / notice-body-text / legal-consequences / payment-instructions / page-footer；纯文本输入，后端自动套回 `<p>` / `<li>` / `<em>` / `<strong>` 等格式（格式规则表 `FORMAT_RULES`）
- **3 张可替换图片 + 每张独立 size/offset/rotation**：logo / firm seal (律所章) / signature_seal (签名章)；PNG 上传 + 4 个数字输入（size/dx/dy/rot）都落到当前模板（`assets_config.json`）；seal/signature_seal 的用户基础角度会和防伪层 per-doc 随机 ±15° 叠加
- **新占位符自动生效**：在 body 里写 `{{foo}}`，Analyze Excel / Manual 模式都会自动把 `foo` 加进占位符列表，按**视觉顺序**（从律师函顶到底）；任何含 `date`（大小写不敏感）的占位符自动用日期选择器
- **金额字段智能交互**：`MONEY_KEYWORDS` 命中的 input blur 时自动千分位 + 2 位小数；`Payable` 只读，实时求和 = Principal + Interest + Penalty
- **固定骨架**：LEGAL NOTICE 标题、SUBJECT 灰条、金额表、双线边框——这些**不可编辑**，保证律所视觉一致

### 安全层（概要，完整细节见 [`ANTI_COUNTERFEIT.md`](ANTI_COUNTERFEIT.md)）
- **双语水印**（英/乌尔都）按 name 哈希种子——每条律师函**随机化纹路 + 随机旋转**，无法 copy-paste 伪造
- **防伪底纹**：正弦曲线 + 阿基米德螺旋 + 同心圆（density 可选 low/medium/high/ultra）
- **QR + 16 位防伪序列号**：QR 位于页 2 **右下角**（距内边框 1.5 cm、距页脚线 2 cm），下方 `XXXX-XXXX-XXXX-XXXX` 格式防伪码，deterministic per-name（同人复跑相同、不同人必不同）
- **红色律所章** + **蓝色签名章**：按 name 种子 ±15° 随机旋转（用户 base 角 + per-doc 随机叠加）
- **权限锁 PDF**：Playwright → PyMuPDF 300 dpi 光栅化（零文字对象）→ pikepdf AES-256（禁复制、禁修改、允许打印 + 元数据）

### 生成层
- **Excel batch（异步）**：`/generate` 立即返回 `task_id`，前端轮询 `/status`，结果 zip 流式下载；ProcessPoolExecutor 多 Chromium 并行；按任意列分组 → 每组独立 zip
- **Manual single（同步）**：`/generate_one` 直接返回 PDF 附件，复用一个共享 Chromium（冷启动 1 s → 复用后每次 300–500 ms），`threading.Semaphore(4)` 限流（超出返 429）；`Content-Disposition` 用 RFC 5987 支持中文 / 乌尔都文等非 ASCII 文件名
- **Machine profiles**：VPS（1 worker）/ Mac（最多 6 worker）
- **历史审计 + 序列号反查**：每条成功生成的 PDF 写一行到 `uploads/history.db`（`serial, name, principal, generated_at`，最多 2M 条 FIFO）；`/verify` 页面按序列号反查真伪
- **两种模式共用同一条渲染流水线**（`render_notice_row_pdf`），区别只在编排层

## 环境要求

- Python 3.9+
- Playwright + Chromium（一次性 ~230 MB）
- 字体：随包 Libre Baskerville / Playfair Display / Arial Unicode；Linux 需 `fonts-noto-nastaliq-urdu` 等

## 本地运行

```bash
pip install -r requirements.txt
playwright install chromium
python legal_notice_gen.py
# http://127.0.0.1:5002
# 默认密码: RT%L6IXoXT*^r=z6%npe （或 LEGAL_NOTICE_PASSWORD 环境变量）
```

## VPS 部署（Ubuntu/Debian）

```bash
scp -r legal_notice_gen/ root@YOUR_VPS:/root/
ssh root@YOUR_VPS
cd /root/legal_notice_gen
sudo bash deploy/setup.sh
```

脚本自动：
1. 安装 Python / Nginx / 字体（Nastaliq Urdu / DejaVu / Noto core）
2. 创建用户 `legalnotice`，部署到 `/opt/legal_notice_gen`
3. 建 venv → `pip install -r requirements.txt` + gunicorn
4. `playwright install-deps chromium` + `playwright install chromium`
5. systemd 服务（**`--workers 1 --threads 16`**）
6. Nginx 反向代理（`client_max_body_size 100M`, `proxy_read_timeout 1800s`）

> ⚠️ **Gunicorn 必须 `--workers 1`**：任务状态 (`TASKS` dict) 驻留单进程内存，`/status` 轮询和 `/download` 必须命中同一进程。并发靠 `--threads` 而不是 `--workers`。

## 单页工作台使用

### 1. Template（默认展开）

- **Active template 下拉**：选择当前操作的模板（`default` + 你保存过的）
- **Rename**：改名（对 `default` 也生效，名字会落到 `uploads/templates/default/meta.json`）
- **Save as new…**：用当前 sidebar 里的正文/水印/图片作为起点，创建新模板（图片 PNG 也会 copy 过去）
- **Reload**：从服务器重取当前模板，丢弃未保存的 sidebar 更改
- **Delete**：删除当前模板（`default` 不可删）

### 2. Body Content

7 个 textarea，对应 7 个 `data-purpose` 块（覆盖页眉到页脚的所有文字区域）：

| 块 | 输入约定 |
|---|---|
| `letterhead-firm` | 行 1 = 律所名（`<h1>`），行 2+ = slogan 行（join 成 `<br>`） |
| `letterhead-partners` | 每行一个合伙人：`Name \| Role`；没有 `\|` 就只有 name |
| `notice-subject` | 单段文本（通常一长行 uppercase） |
| `notice-body-text` | 段落用空行分隔；`[[AMOUNTS_TABLE]]` = 金额表位置；`[[CALLOUT]]` = 标记下一段为"you are hereby called upon..."样式 |
| `legal-consequences` | 每行一条后果，自动编号 1/2/3… |
| `payment-instructions` | 段落用空行分隔 |
| `page-footer` | 每行一个 `<p>`；`Office:` / `Email:` / `Phone:` 自动加粗，`\|` 保留原样 |

> 签名区（页 2 右下）原本是"打印版合伙人名 + 职位 + 蓝色签名章"两个组件，现在改为**一张图**——签名/印刷体姓名/职位全部烘焙进 `signature_seal` PNG，所以不再有 `signature-block` 文本块。要改签名律师，上传新 PNG 即可。

**格式托管**：`M/s Zanda Financial Services (Pvt.) Limited` / `MoneyTap Application` / `03 (three) days` / `{{Payable}}` / `Office:` / `Email:` / `Phone:` 这类短语在代码里登记为自动斜体 / 下划线 / 加粗，用户**从不需要写 HTML 标签**。若你编辑文本时保留这些短语原样，格式自动加回；若改字了，格式就按新文本的语义套（没命中则纯文本）。

`page-footer` 的 inner HTML 会同时替换到两页的 `<div class="contact">`，一次编辑两页同步。

### 3. Images

3 张图，每张 **PNG RGBA 透明底**，**一个模板一套**。除了上传 PNG 覆盖出厂默认图之外，每个槽位下面还有 4 个 number input 调节"放置":

| 槽位 | 建议尺寸 | 风格 | 默认 size | size 范围 | rot 范围 |
|---|---|---|---|---|---|
| `logo` | 1000×1000 | 顶部圆框内（`border-radius: 50%`），金色/彩色主体居中留 ≥15% 黑/透明安全边防止被圆裁切 | **22 mm** | 12–30 mm | ±30° |
| `seal` | 1000×1000 | 律所圆形公章（默认红色 "S&S LAW FIRM"），透明底 | **22 mm** | 16–36 mm | ±30° |
| `signature_seal` | 1000×1000 | 手写签名 + 印刷体姓名 + 职位**一张方图**（默认蓝色"Muhammad Junaid Abbasi / Advocate High Court / (Managing Partner)"） | **44 mm** | 40–80 mm | ±30° |

每张图可调：`size`（mm）/ `rotation`（°，基础角）/ `offset X` / `offset Y`（mm）。数值存到模板的 `assets_config.json`，渲染时作为 CSS vars 注入。**`seal` / `signature_seal` 的用户 rot 是"基础角"**，最终角度 = 用户 rot + 防伪层的 per-doc 随机 ±15°；`logo` 无随机抖动，用户 rot 就是最终角度。

- "Reset image" 撤销 PNG 上传，退回出厂默认图
- "Reset placement" 撤销数值调整，回到 save 前的状态（Save placement 后下次 load 才生效）

### 4. Watermark & Pattern

- 水印：enabled / 英文模板 / 乌尔都语模板 / 字号 10–64 px / 透明度 0–100% / 数量 1–100 / 墨水色
- 底纹：enabled / 透明度 0–100% / 密度 `low / medium / high / ultra`（对应 20 / 40 / 60 / 80 条曲线）

保存到当前模板的 `security.json`。

### 5. Generate

1. 上传 Excel（`.xlsx`）
2. 点 Analyze → 看占位符匹配情况
3. 未匹配的占位符 → textarea 手填（所有行共享）；`date` 走日期选择器
4. Filename columns → 勾选一列或多列，按 `_` 拼接
5. Group by → 可选，把相同值的行打到一个 zip
6. Machine profile → VPS 1 worker / Mac 6 worker
7. Generate → 进度条 → 自动下载 zip

## 占位符约定

默认模板占位符（一对一对应 Excel 表头）：

| 占位符 | 典型列 |
|---|---|
| `{{date}}` | 律师函日期（网页日期选择器覆盖） |
| `{{name}}` | 被告姓名 |
| `{{cnic}}` | CNIC |
| `{{phone}}` | 手机 |
| `{{disb_date}}` | 放款日 |
| `{{Due_date}}` | 到期日 |
| `{{Principal_Amount}}` | 本金 |
| `{{Interest}}` | 利息 |
| `{{Penalty}}` | 罚金 |
| `{{Payable}}` | 应付总额 |
| `{{Transaction_id}}` | 交易 ID |
| `{{easypaisa_account}}` | EasyPaisa 账号 |

自定义模板若在 Body 里加了新 `{{foo}}` 占位符，Analyze Excel 时会一并扫出来。

## API 参考

### 模板 CRUD

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/templates` | 列表 `[{slug, name, builtin, created, updated}, ...]` |
| `GET` | `/api/templates/<slug>` | 读 `{slug, name, builtin, blocks, security, assets:{logo:bool, seal:bool, signature_seal:bool}, assets_config}` |
| `PUT` | `/api/templates/<slug>` | 改（body: `{name?, blocks?, security?, assets_config?}`），未传字段保持不变 |
| `POST` | `/api/templates` | 新建（body: `{name, base_slug?, blocks?, security?, assets_config?}`；继承 base 的图片 + 放置数值） |
| `DELETE` | `/api/templates/<slug>` | 删除（`default` 拒绝） |
| `POST` | `/api/templates/<slug>/assets/<kind>` | 上传 PNG（form-data `file`；kind ∈ `logo, seal, signature_seal`） |
| `DELETE` | `/api/templates/<slug>/assets/<kind>` | 退回出厂默认图 |
| `GET` | `/api/templates/<slug>/assets/<kind>` | 取图（有覆盖返覆盖，否则出厂默认） |
| `GET` | `/api/templates/<slug>/placeholders` | 返回 `{placeholders:[...,按视觉顺序], money:[...]}`；Manual 模式用来渲染 input 列表 |

### 生成任务

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/` | 单页工作台 |
| `GET` | `/preview.html?slug=<slug>&fill=0` | iframe 预览（默认 `fill=0` 显示字面量占位符；`fill=1` 用样本数据） |
| `POST` | `/api/preview_html` | 未保存状态的实时预览：body `{slug, blocks?, security?, assets_config?, fields?}` → HTML；Manual 模式传 `fields` 走 partial-fill |
| `POST` | `/upload` | Excel 模式：`excel=<file>` + `template_slug=<slug>` → 占位符分析 |
| `POST` | `/generate` | Excel 模式批量：JSON `{manual_fields, filename_fields, group_by_field, date_value, machine}` → `{task_id}` |
| `GET` | `/status/<task_id>` | Excel 模式轮询：`{status, stage, progress, total, ready_parts, ...}` |
| `GET` | `/download/<task_id>/<part_index>` | Excel 模式下载一组生成好的 zip |
| `POST` | `/generate_one` | Manual 模式：JSON `{template_slug, fields}` → PDF 流式附件（`Content-Disposition` 用 RFC 5987，非 ASCII 文件名友好）；共享 Chromium + 信号量限流 4 并发，超限返 429 |

### 历史 / 反查（2026-04-16 新增）

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/verify` | UI 页面：输入 16 位序列号反查真伪 + 查看最近历史表 |
| `GET` | `/api/verify?serial=...` | 返回 `{ok, name, principal, generated_at}` 或 404 |
| `GET` | `/api/history?limit=&offset=&q=` | 分页列表，`q` 按姓名模糊搜 |

数据存 `uploads/history.db`（SQLite + WAL）。容量上限 2,000,000 条，超出自动 FIFO 删最旧。每次 Excel / Manual 生成成功都写一行（失败行不记录）。

**完整的查询 API 规范（请求参数、响应字段、错误码、序列号格式、未来扩展预留、数据模型）见 [`INQUIRY_API.md`](INQUIRY_API.md)**——独立文档以便未来接入第三方系统（OA、移动 App、合作方核验服务）。

### 认证

| 方法 | 路径 |
|---|---|
| `GET / POST` | `/login` |
| `GET / POST` | `/logout` |

## 存储布局

```
legal_notice_gen/
├── legal_notice_gen.py             # 主程序（后端 + 内联 HTML_TEMPLATE）
├── requirements.txt
├── templates/
│   ├── legal_notice_full.html      # 基础布局（双边框 / 字体 / 网格 / 页脚 ……不由用户改）
│   └── static/
│       ├── fonts.css
│       ├── fonts/                  # Libre Baskerville / Playfair Display / EB Garamond TTFs
│       └── images/                 # logo.png / seal_default.png / signature_seal_default.png
├── deploy/
│   └── setup.sh                    # VPS 一键部署
├── uploads/
│   ├── .secret_key                 # Flask session secret，持久化避免重启丢 session
│   ├── default_security.json       # 旧版本遗留（现仍用作兜底默认）
│   ├── templates/
│   │   ├── default/                # 只有当用户编辑默认模板时才出现
│   │   │   ├── meta.json           # {name, created, updated}
│   │   │   ├── blocks.json         # 覆盖默认 7 块正文（含 letterhead / footer）
│   │   │   ├── security.json       # 覆盖默认水印 / 底纹
│   │   │   ├── assets_config.json  # logo/seal/signature_seal 的 size/dx/dy/rot
│   │   │   └── assets/
│   │   │       ├── logo.png        # 可选覆盖出厂 logo
│   │   │       ├── seal.png
│   │   │       └── signature_seal.png
│   │   └── <user-slug>/            # "另存为新模板" 创建的模板
│   │       └── ...  （同上结构）
│   ├── <session-id>/               # /upload 临时目录（Excel 数据）
│   ├── task_*.pdf                  # 渲染中间产物（自动清理）
│   └── part_*.zip                  # 已生成待下载的分组 zip（5 分钟 TTL）
└── readme.md
```

## 防伪 / 防篡改原理

1. **每文档唯一化**：水印/底纹/印章旋转全用 `hash(name)` 作为 RNG 种子；同名复跑结果一致，不同名一定不同——复制别人律师函当模板对不上
2. **文字 = 像素**：PyMuPDF 300 dpi 渲图重组 PDF，每页只剩一张图；任何 PDF 编辑器都选不中、改不动文字
3. **硬权限位**：pikepdf AES-256，owner 密码是随机 32 字节没人持有。用户密码空串（开门见山可打开），但 `extract / modify` 被系统级拒绝
4. **元数据水印**：Producer、Title、CreationDate 写入 PDF 头，律所可按 filename hash 反查真伪

## 架构说明

- 整个工具**一个 Python 文件**（~2850 行）：Flask 路由 / 安全层 / 模板 CRUD / 渲染流水线 / 内联 HTML_TEMPLATE 全在一起
- 按"单文件交付"原则：部署就 scp 一个 .py + requirements.txt + templates 目录
- 任务状态在进程内存（`TASKS` dict），**所以 Gunicorn 必须 1 worker**；要跨进程/跨重启持久化任务需引入 Redis/Celery
- 渲染并发靠 `ProcessPoolExecutor`，每 worker 自带一个 Chromium 实例（长期复用，避免每次启动 ~1s 开销）

## 规模性能（M2 Max · Mac profile · 6 worker）

- 1 条：~4 秒
- 50 条：~45 秒
- 500 条：~6 分钟
- 5000 条：~1 小时

VPS profile（1 worker）约为 6 倍慢。需要更快可以把 Chromium 升级到 Persistent Browser Context + 共享 pool（TODO）。

## 已知事项

- 进程内存存任务状态——重启服务后进行中的任务会丢；完成状态 1 小时后过期从 TASKS 里清理
- 模板大改**不自动同步**到已保存的自定义模板（每个模板是一份独立快照）
- 格式托管规则（`FORMAT_RULES`）目前硬编码在代码里；新增加粗/斜体短语要改 `legal_notice_gen.py`
