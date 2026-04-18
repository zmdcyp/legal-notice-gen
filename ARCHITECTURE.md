# 架构与 VPS 部署方案

配套 [`DEPLOY.md`](DEPLOY.md) （操作手册）和 [`readme.md`](readme.md)（功能/API 细节）。本文回答"它是怎么搭起来的"和"上生产该怎么摆"。

---

## 1 · 定位

一台律所内部工具，三条主生产流程：

| 流程 | 输入 | 输出 | 规模 | 场景 |
|---|---|---|---|---|
| Excel 批量 | 一次性上传 `.xlsx` | 按分组打包的 zip | 5 000 – 10 000 条 | 一次性大派发 |
| Manual 单条 | 手填一组占位符 | 单 PDF 附件 | 1 条 | 给特定客户定制 |
| Inventory 存量 | 导入几十万条入库，按需筛选 | 外层日批次 zip，内层按负责人分子 zip | 200 – 5 000 条/批 | 每日挑 N 条发函 |

三条路径**共用同一渲染流水线**（`render_notice_row_pdf` → Chromium → PyMuPDF 光栅化 → pikepdf AES-256 加锁），区别只在编排层。

此外还有两条辅助路径：

| 辅助 | 输入 | 输出 | 场景 |
|---|---|---|---|
| 内部验真 | `/verify` + 16 位序列号 | 原始 `{name, principal, generated_at}` | 律所自检 / 内部对账 |
| 公开验真 | `/public/verify` + 序列号 + CAPTCHA | 同上 | 收函人扫码自证真伪 |

---

## 2 · 系统架构

### 2.1 组件拓扑（生产 VPS）

```
┌──────────────────────────────────────────────────────────────────┐
│                          公网入口                                  │
│                                                                  │
│  ┌──────────────┐     ┌──────────────┐    ┌───────────────────┐ │
│  │ 律所员工浏览器 │     │ 收函人手机     │    │ Cloudflare        │ │
│  │（登录 + 内部） │     │（扫 QR 验真） │    │ Turnstile 验证    │ │
│  └───────┬──────┘     └───────┬──────┘    └─────────▲─────────┘ │
│          │ HTTPS               │ HTTPS                │ 服务端    │
│          ▼                     ▼                      │ siteverify│
└──────────┼─────────────────────┼──────────────────────┼──────────┘
           │                     │                      │
  ┌────────┴─────────────────────┴──────────────────────┴──────────┐
  │                 VPS (Ubuntu 22.04 / 24.04)                      │
  │                                                                 │
  │  ┌─────────────────────────────────────────────────────────┐   │
  │  │ Nginx 80/443                                            │   │
  │  │  · TLS termination（Let's Encrypt 或 Cloudflare origin）│   │
  │  │  · client_max_body_size 100M（Excel 大文件）            │   │
  │  │  · proxy_read_timeout 1800s（批量生成长流）              │   │
  │  │  · X-Forwarded-For → 给 Flask 真实客户端 IP             │   │
  │  │  · limit_req_zone（/login + /api/public/verify）        │   │
  │  └──────────────────────────┬──────────────────────────────┘   │
  │                             │ 127.0.0.1:5002                    │
  │                             ▼                                    │
  │  ┌─────────────────────────────────────────────────────────┐   │
  │  │ Gunicorn (systemd)                                      │   │
  │  │  · --workers 1 --threads 16 --worker-class gthread      │   │
  │  │  · --timeout 1800 --graceful-timeout 60                 │   │
  │  │  · 单进程（必须，因为状态在内存里，见 §2.2）              │   │
  │  └──────────────────────────┬──────────────────────────────┘   │
  │                             │                                    │
  │                             ▼                                    │
  │  ┌─────────────────────────────────────────────────────────┐   │
  │  │ Flask app (legal_notice_gen.py · 单文件 ~7 k 行)         │   │
  │  │  ┌─────────────┐  ┌─────────────┐  ┌──────────────────┐│   │
  │  │  │ Web 路由    │  │ 渲染流水线   │  │ TASKS 字典      ││   │
  │  │  │（HTML/API） │  │ 内嵌 Chromium│  │（内存, 异步任务 ││   │
  │  │  └──────┬──────┘  │ 池 + LRU   │  │  状态机）        ││   │
  │  │         │         │ 缓存）      │  └──────────────────┘│   │
  │  │         │         └──────┬──────┘                       │   │
  │  │         │                │ ProcessPoolExecutor          │   │
  │  │         │                ▼                               │   │
  │  │         │          ┌─────────────────┐                  │   │
  │  │         │          │ worker 1..N     │                  │   │
  │  │         │          │ 每个 worker     │                  │   │
  │  │         │          │ 自带一个长存     │                  │   │
  │  │         │          │ Chromium       │                  │   │
  │  │         │          └─────────────────┘                  │   │
  │  │         │                                                │   │
  │  └─────────┼────────────────────────────────────────────────┘   │
  │            │                                                    │
  │            ▼                                                    │
  │  ┌─────────────────────────────────────────────────────────┐   │
  │  │ 持久化层（本地文件系统，SQLite WAL）                      │   │
  │  │  · uploads/cases.db       案件主表（Inventory）           │   │
  │  │  · uploads/history.db     生成历史 + 查询审计             │   │
  │  │  · uploads/templates/     模板 overlay（blocks / assets）│   │
  │  │  · uploads/inventory_batches/    持久化外层批次 zip       │   │
  │  │  · templates/static/      出厂字体 / logo / seal PNG     │   │
  │  └─────────────────────────────────────────────────────────┘   │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

### 2.2 为什么是"单进程"

这是项目最关键的设计约束——**Gunicorn 必须 `--workers 1`**。原因：下面这四项状态全部活在 Python 进程内存里：

| 内存中状态 | 作用 | 多进程会怎样 |
|---|---|---|
| `TASKS: dict` | 异步任务进度 / 就绪 zip 路径 | Worker A 启任务、Worker B 轮询 `/status` → 404 |
| `_SHARED_SINGLE_BROWSER` | Manual 模式共享 Chromium | 每个 Worker 各起一个，浪费 300 MB × N |
| `_MATH_CAPTCHA: dict` | 公开验真 CAPTCHA token | Worker A 发 token、Worker B 收 answer → 拒绝 |
| `_PUBLIC_RL: dict` | IP 限流滑动窗口 | 多进程各自计数 → 实际配额翻 N 倍 |
| `@lru_cache` | QR/水印图缓存 | 每个 Worker 独立缓存（无大害但多占内存） |

并发靠 `--threads 16`（同进程内多线程），足以应付 "5 000 条批量 + 几个用户同时在线" 这个量级。再往上需要真正的分布式改造（见 §10）。

### 2.3 并发模型

请求到达后，按类型进入不同的并发机制：

```
请求 ─┬─→ 同步 HTTP（登录 / 模板 CRUD / 搜索 / 查询 / 状态） 立即响应
     ├─→ 单 PDF 渲染（Manual, /generate_one）
     │     threading.Semaphore(4) 限流 + _SHARED_SINGLE_BROWSER 共享
     │     → 超 4 并发直接 429
     └─→ 批量任务（/generate, /api/cases/generate）
           · 立即返 task_id
           · threading.Thread 开工
             ↓
           generate_notices_html
             ↓
           ProcessPoolExecutor(max_workers=N)
             · 每个子进程 _html_worker_init() 启一个 Chromium
             · 主进程只负责编排 + zip 打包
             ↓
           前端轮询 /status/<task_id> 看进度
             ↓
           完成 → ready_parts 填充 → /download/<task_id>/<idx> 拉 zip
```

### 2.4 故障边界

- **渲染失败**：单条 PDF 异常被捕获，不会拖垮整批；记进 `TASKS[...]['error']` 前端可见
- **Chromium 崩溃**：`ProcessPoolExecutor` 内 worker 进程独立，一个挂掉剩下的继续
- **CAPTCHA / Turnstile 服务不可达**：`_verify_turnstile_token` 返 False，请求 403，不会阻塞
- **日志写失败**：`_log_verify_query` try/except 吞掉错误，不影响主路径
- **磁盘满**：SQLite WAL 会回滚写入，渲染失败；Nginx 返 502；systemd 重启不会修，需要人工清盘

---

## 3 · 模块与职责

单文件 `legal_notice_gen.py` 大约 7 000 行，分段大致如下（在文件里按顺序找）：

| 区段 | 职责 | 关键符号 |
|---|---|---|
| `## app setup` | Flask app 实例 + 秘钥持久化 + 工作目录 | `app`, `UPLOAD_DIR` |
| `## access auth` | 密码登录、session 网关 | `APP_PASSWORD`, `_require_auth`, `_AUTH_EXEMPT_PATHS` |
| `## machine profiles` | VPS/Mac 并发档位 | `MACHINE_PROFILES`, `_get_machine_profile` |
| `## templates storage` | 默认模板 + overlay 管理 | `DEFAULT_BLOCKS_TEXT`, `load_template`, `TEMPLATES_LOCKED` |
| `## block rendering` | 纯文本 → HTML 片段 | `_render_body_text`, `FORMAT_RULES` |
| `## async tasks` | 进程内任务状态机 | `TASKS`, `_new_task`, `_update_task` |
| `## security overlay` | 水印 + 底纹 + QR | `_render_security_overlay_png`, `_render_qr_png` |
| `## history DB` | 生成审计 + 查询审计 | `_init_history_db`, `_log_notice_record`, `_log_verify_query` |
| `## public verify` | Turnstile / 数学题 / 限流 | `_verify_turnstile_token`, `_new_math_challenge`, `_public_rate_check` |
| `## cases store` | Inventory 案件库 | `_init_cases_db`, `_extract_display_extras`, `_wrap_inventory_batch` |
| `## render pipeline` | HTML → PDF 的核心编排 | `build_notice_html`, `render_notice_row_pdf`, `generate_notices_html` |
| `## routes` | 所有 HTTP endpoints | `@app.route(...)` |
| `## HTML templates` | 内联前端（workbench / verify / inventory / public-verify / login） | `HTML_TEMPLATE`, 等 |

---

## 4 · 数据存储

### 4.1 SQLite 数据库（都在 `uploads/`）

所有 DB 都开 **WAL 模式**——读写可并发，审计日志在写的时候 `/verify` 仍能读。

#### 4.1.1 `cases.db` — 案件主表

```sql
CREATE TABLE cases (
    order_id      TEXT PRIMARY KEY,    -- 订单编号（业务唯一键）
    cnic          TEXT,                 -- 索引列（身份证）
    name          TEXT,                 -- 索引列（当前姓名，可内联编辑）
    original_name TEXT,                 -- 导入时的原名（永不触碰）
    phone         TEXT,                 -- 索引列（手机号）
    row_json      TEXT NOT NULL,        -- 整行 Excel 数据（JSON）
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX idx_cases_cnic  ON cases(cnic);
CREATE INDEX idx_cases_name  ON cases(name);
CREATE INDEX idx_cases_phone ON cases(phone);
```

**设计要点**：
- `order_id` 用 `TEXT` 而非 `INTEGER`——真实数据里是 19 位 bigint，超 SQLite 整型 8 字节上限
- `row_json` 冗余存整行——兼容 Excel 任意加列，渲染器直接消费；`cnic/name/phone` 单列只为搜索索引
- 多值精确匹配走临时表 JOIN（`_cases_temp_values_table`），绕过 SQLite 999 参数上限

**访问模式**：
- 写：只通过异步导入 task（`_cases_import_worker`）；批量 1000 行一次 commit
- 读：分页搜索、批量 by_ids、bulk_match、generate 时拉 row_json

#### 4.1.2 `history.db` — 审计

两张表合住一个 DB 文件：

```sql
-- 生成审计：每成功渲染一封记一条
CREATE TABLE notice_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    serial        TEXT NOT NULL,       -- 16 位防伪序列号
    name          TEXT NOT NULL,
    principal     TEXT,
    generated_at  TEXT NOT NULL
);
-- 容量：2 000 000 条 FIFO，每天 5000 条约够 400 天
-- Excel batch、Inventory、Manual 三条路径都 hook

-- 查询审计：每次 /verify 或 /api/public/verify 记一条
CREATE TABLE verify_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    source  TEXT NOT NULL,             -- 'public' | 'internal'
    ip      TEXT,                       -- 真实客户端 IP（靠 X-Forwarded-For）
    serial  TEXT NOT NULL,
    hit     INTEGER NOT NULL,           -- 1 = 查到 / 0 = 未找到
    ua      TEXT
);
-- 容量：500 000 条 FIFO
```

`verify_log` 的作用：除了取证，也是滥用检测依据——某 IP 在 1 小时内尝试 80 次 `hit=0` 就是在暴力扫 serial。

### 4.2 文件系统

```
/opt/legal_notice_gen/               ← 生产 APP_DIR
├── legal_notice_gen.py              ← 主程序
├── requirements.txt
├── templates/                       ← 代码随包（不要往里面写）
│   ├── legal_notice_full.html       ← 基础布局（双边框 / 字体 / 骨架）
│   └── static/
│       ├── fonts.css + fonts/       ← Libre Baskerville / Playfair
│       └── images/                  ← 出厂 logo / seal / signature_seal
│                                       + logo_mono.png（/verify 等页用）
│
└── uploads/                         ← 运行时生成的数据（**全部要备份**）
    ├── .secret_key                  ← Flask session 秘钥
    ├── cases.db*                    ← 案件主表（+ WAL / SHM）
    ├── history.db*                  ← 审计（+ WAL / SHM）
    │
    ├── templates/
    │   └── default/                 ← 内置模板的 UI 编辑 overlay
    │       ├── blocks.json
    │       ├── security.json
    │       ├── assets_config.json
    │       └── assets/logo.png, seal.png, signature_seal.png
    │
    ├── case_imports/                ← Inventory import 两阶段临时文件
    │   └── <token>.xlsx             （preview 阶段的 xlsx 暂存，commit 后自删）
    │
    ├── inventory_batches/           ← **持久化** 批次 zip
    │   └── YYYY-MM-DD_batch-NNN.zip
    │
    ├── <session-id>/                ← Excel batch 临时工作区（自清）
    ├── part_*.zip                   ← Excel batch 分组 zip（10 分钟 TTL）
    └── task_*.pdf                   ← 渲染中间产物（自清）
```

**备份必备项**（丢了无法重建）：
- `uploads/cases.db` + WAL/SHM —— Inventory 主数据
- `uploads/history.db` + WAL/SHM —— 审计链
- `uploads/templates/` —— 用户对模板的编辑
- `uploads/inventory_batches/` —— 已交付的归档

**可丢**（重启/下次生成自动重建）：
- `.secret_key`（丢了只是让当前 session 失效，不影响数据）
- `case_imports/` + `part_*.zip` + `<session-id>/` + `task_*.pdf`

---

## 5 · 关键数据流

### 5.1 Excel 批量生成

```
浏览器                        Flask                          子进程池
──────                        ─────                          ───────
/upload ──Excel──────────→ 存 uploads/<sid>/data.xlsx
                           扫表头、对占位符
                        ←── {placeholders, missing, preview}
/generate ──{...}─────────→ _new_task → task_id
                           threading.Thread:
                              generate_notices_html
                                ↓ 按 group_by 分组
                                ↓ ProcessPool.submit ───────→ _html_worker_job
                                                                build_notice_html
                                                                _playwright_render
                                                                _rasterize_pdf
                                                                _apply_pdf_lock
                                                                _log_notice_record
                                                             ←─ PDF bytes
                                ↓ 收齐一组 → zip → ready_parts
                        ←── {task_id}
/status/<tid> (轮询) ──────→ TASKS[tid] 公开字段
/download/<tid>/<part> ───→ send_file(ready_parts[idx].path)
                           Timer(600s) 延迟删 zip（non-persistent）
```

### 5.2 存量选择生成

```
/api/cases/import/preview ──Excel──→ 扫 订单编号 + 冲突计数
                                 ←── {token, new, conflict, headers, ...}
/api/cases/import/commit ──{token, skip|overwrite}──→ _new_task
                                                      threading.Thread:
                                                         read_excel
                                                         事务分批 1000 行 upsert
                                                      ←── {task_id} → 复用 /status

/api/cases/search ──{field, q, page}──→ 三模式查询
                                     ←── {rows[...含负责人+金额]}
/api/cases/bulk_match ──{excel | values}──→ TEMP 表 JOIN
                                         ←── {matched_ids, missing_sample}

/api/cases/generate ──{order_ids, grouped}──→ _cases_fetch_rows (from DB)
                                              date 覆盖
                                              generate_notices_html
                                              ↓
                                              _wrap_inventory_batch
                                                  ├── grouped=T: 嵌套 batch.zip
                                                  │              / <负责人>.zip
                                                  │              / <pdf>
                                                  └── grouped=F: 平铺 batch.zip / <pdf>
                                              写 inventory_batches/ (persistent=True)
                                           ←── {task_id} → /status → /download
```

### 5.3 内部验真（登录后）

```
GET /verify ──→ HTML 页（三块：查询表单 / Recent History / Recent Verify Queries）
POST /api/verify ──{serial}──→ lookup_notice_by_serial (history.db)
                                _log_verify_query(source='internal', hit=...)
                             ←── {ok:true, name, principal, generated_at} or 404
GET /api/history?q=... ──→ 分页列表
GET /api/verify_log?source=... ──→ 分页审计
```

### 5.4 公开验真（扫码，无登录）

```
手机扫 QR
  payload = https://v.sspak.law/public/verify?serial=XXXX-XXXX-XXXX-XXXX
  ↓
浏览器打开
  → GET /public/verify?serial=...
    HTML 返回（无需 session）
    JS 解析 URL → 预填 serial 框 → 焦点到 CAPTCHA

[math 模式]
  GET /api/public/verify/challenge ──→ 限流检查
                                        _new_math_challenge (token+answer 进内存)
                                    ←── {token, question}
  用户输入答案
  POST /api/public/verify ──{serial, captcha_token, captcha_answer}──→
                                        格式检查 → 400
                                        限流检查 → 429
                                        _verify_math_answer (消费 token)
                                        lookup_notice_by_serial
                                        _log_verify_query(source='public')
                                    ←── {ok:true|false, ...}

[Turnstile 模式]
  页面加载 Cloudflare Turnstile widget (data-sitekey=...)
  用户通过挑战 → widget 回调 → 本地拿到 turnstile_token
  POST /api/public/verify ──{serial, turnstile_token}──→
                                        格式检查 → 400
                                        限流检查 → 429
                                        urllib POST https://challenges.cloudflare.com/
                                          turnstile/v0/siteverify (secret + token + ip)
                                        lookup_notice_by_serial
                                        _log_verify_query(source='public')
                                    ←── {ok:true|false, ...}
```

---

## 6 · 安全模型

### 6.1 认证层

| 路径族 | 认证 | 理由 |
|---|---|---|
| `/login`, `/logout`, `/static/*` | 免 | 登录入口 + 字体/图片公开资源 |
| `/public/verify`, `/api/public/verify*` | 免，但过 CAPTCHA + 限流 | 收函人自证 |
| 其他全部 (`/`, `/inventory`, `/verify`, `/api/*`) | 密码 session | 律所内部操作 |

中间件 `_require_auth`（`@app.before_request`）统一拦截：
- API 路径 → 401 JSON
- 其他 → 302 → `/login`

密码对比走 `hmac.compare_digest`，常量时间、无时序泄漏。

### 6.2 公开端点防滥用（两层独立）

```
请求进来
  │
  ├── (1) 格式校验 → 序列号不是 XXXX-XXXX-XXXX-XXXX → 400 （早于一切）
  │
  ├── (2) 限流（per-IP 滑动窗口 5/分 · 100/天）
  │       超 → 429（此时 CAPTCHA 都没被调用）
  │
  ├── (3) CAPTCHA 验证
  │       Turnstile：urllib POST Cloudflare siteverify
  │       math：token 查字典 + 消费（成功/失败都删）
  │       不通过 → 403
  │
  └── (4) lookup + 记审计 → ok:true / ok:false (都是 200)
```

**关键决策**：
- "查不到" **不** 返 404（返 200 + `{ok:false}`），让状态码无法做枚举探测
- CAPTCHA token **永远单次**——就算答错也消费，防止暴力答数学题
- 限流 IP 取自 `X-Forwarded-For` 第一跳——**Nginx 必须正确转发**，否则所有请求都来自 `127.0.0.1`、整个防护失效

### 6.3 审计链

生成侧（`notice_history`）和查询侧（`verify_log`）两条独立：

```
律所发信 ──生成审计──→ (serial, name, principal, ts)
                                  ↓
                            /verify, /public/verify
                                  ↓
                            查询审计 ──→ (ts, source, ip, serial, hit, ua)
```

律所能同时知道：这封信是真的发出去过（`notice_history.serial` 查得到） + 谁/什么时候在查（`verify_log.ip/ua/ts`）。

### 6.4 模板锁定

`TEMPLATES_LOCKED = True` 的含义：
- **对象层面锁死**：不能新建（POST /api/templates 返 403）、不能删除（DELETE 返 403）、不能改名（PUT 里 `name` 字段静默丢弃）
- **内容层面开放**：body blocks / 水印 / 图片 / 放置参数 全都可以通过 UI 改，持久化到 `uploads/templates/default/` overlay，重启不丢
- **启动清理**：`_cleanup_non_builtin_template_dirs()` 只清非 default/ 的残留目录，default/ 保留

用场景：律所统一视觉规范不希望员工误删改名生成第二份模板，但允许调正文和印章位置。

---

## 7 · 部署拓扑

### 7.1 标准单 VPS 布局

```
DNS:
  v.sspak.law      A  →  <VPS_IP>   （公开验真专用子域）
  app.sspak.law    A  →  <VPS_IP>   （员工工作台）

Cloudflare（可选）:
  Proxy ON       → DDoS 防护 + 隐藏源 IP + 免费 SSL 到边缘
  Cache bypass   → /api/*  /generate  /download  /upload  /public/verify*

VPS:
  ufw: allow 22/tcp, 80/tcp, 443/tcp
  Nginx: 两个 server block → 都 proxy_pass 到同一个 127.0.0.1:5002

  systemd: legal_notice_gen.service
    User=legalnotice
    Environment="LEGAL_NOTICE_PASSWORD=..."
    Environment="VERIFY_BASE_URL=https://v.sspak.law/public/verify"
    Environment="TURNSTILE_SITE_KEY=..."
    Environment="TURNSTILE_SECRET_KEY=..."

  数据目录: /opt/legal_notice_gen/uploads/  → 每晚 rsync 到外部备份点
```

### 7.2 请求路径

```
员工:
  浏览器 ──HTTPS──→ Cloudflare ──→ Nginx :443
                                      │
                                      │ proxy_set_header X-Forwarded-For …
                                      ▼
                                    127.0.0.1:5002 (Gunicorn)
                                      ↓
                                    Flask (_require_auth)
                                      → 业务逻辑

收函人:
  扫 QR 得 https://v.sspak.law/public/verify?serial=...
  手机浏览器 ──HTTPS──→ Cloudflare ──→ Nginx :443
                                          │
                                          │ 同上 (X-Forwarded-For)
                                          ▼
                                        /public/verify (免登录)
                                          → CAPTCHA + 限流 + lookup
```

### 7.3 Nginx 关键配置

```nginx
# /etc/nginx/sites-available/legal_notice_gen

# 限流 zone（/http 块里定义一次）
limit_req_zone $binary_remote_addr zone=login:10m   rate=10r/m;
limit_req_zone $binary_remote_addr zone=pubvrfy:10m rate=20r/m;

server {
    listen 443 ssl http2;
    server_name app.sspak.law v.sspak.law;

    ssl_certificate     /etc/letsencrypt/live/app.sspak.law/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/app.sspak.law/privkey.pem;

    client_max_body_size 100M;

    # ── 登录端点 → 加额外 rate limit（防暴力猜密码）
    location = /login {
        limit_req zone=login burst=5 nodelay;
        proxy_pass http://127.0.0.1:5002;
        include proxy_headers.conf;
    }

    # ── 公开验真端点 → 单独 rate limit（代码里还有更细的按 IP 限流做二道保护）
    location ~ ^/api/public/verify {
        limit_req zone=pubvrfy burst=10 nodelay;
        proxy_pass http://127.0.0.1:5002;
        include proxy_headers.conf;
    }

    # ── 其他全量转发
    location / {
        proxy_pass http://127.0.0.1:5002;
        include proxy_headers.conf;
        proxy_read_timeout 1800s;   # /download 大 zip 流时间
        proxy_send_timeout 1800s;
        proxy_buffering off;        # 让流式下载真的是流式
    }
}

# 80 → 443 重定向
server {
    listen 80;
    server_name app.sspak.law v.sspak.law;
    return 301 https://$host$request_uri;
}
```

`/etc/nginx/proxy_headers.conf`（所有 location 共用）：
```nginx
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

**没有这三个 header，App 里的 `_client_ip()` 就退化到 `127.0.0.1`，IP 限流全部失效。**

---

## 8 · VPS 部署方案（从 0 到 prod）

下面按时间顺序列每一步。配套 `deploy/setup.sh` 自动做了第 5–7 步的大半；这里写的是完整手动流程，遇到脚本没覆盖的定制需求时参考。

### 8.1 资源配比

| 规模 / 用法 | vCPU | RAM | 磁盘 | Machine Profile |
|---|---|---|---|---|
| 演示 / 小批量（< 200 条/天） | 1 | 2 GB | 20 GB | VPS（1 Chromium） |
| **推荐起步**（< 5 000 条/天 + Inventory 几万条） | 2 | 4 GB | 40 GB | VPS（1 Chromium） |
| 大批量（日 5 000–20 000 条） | 4 | 8 GB | 80 GB | 自定义 2–3 worker |
| 极限（一次 > 20 000 条） | 4–8 | 16 GB | 160 GB | 要改代码去进程内存依赖 |

磁盘里能快速膨胀的是 `inventory_batches/`（持久化不自动清）——规划 **每天平均 1–2 GB**。

### 8.2 域名与 HTTPS

```bash
# A: 两个子域都指向 VPS_IP（DNS provider 里手动加）
app.sspak.law    A   <VPS_IP>
v.sspak.law      A   <VPS_IP>

# B: Let's Encrypt（推荐，自动续期）
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d app.sspak.law -d v.sspak.law
# 会自动改 /etc/nginx/sites-available/* 并装好 systemd timer 续期

# B-alt: Cloudflare 代理 + CF Origin Cert（更省心）
# Cloudflare 控制台 → SSL/TLS → Origin Server → Create Certificate
# 把 cert + key 落到 VPS 上，Nginx 指过去；Proxy Status = Proxied
```

### 8.3 注册 Cloudflare Turnstile（强烈推荐）

数学题 CAPTCHA 扛不住真实的机器人攻击；上生产一定切 Turnstile。

1. [dash.cloudflare.com](https://dash.cloudflare.com) → Turnstile → Add site
2. **Sitekey type = Managed**（默认）、**Widget mode = Invisible** 或 **Managed**
3. Hostnames 填 `v.sspak.law`（或 `*.sspak.law`）
4. 保存后拿到两个 key：`0x4AAAA...` (site) 和 `0x4AAAA...` (secret)
5. 写入 systemd env（见 §8.5）

如果是国内 VPS 访问 Cloudflare 有阻碍，也可以只用数学题 fallback 作为起步，后面再切。代码逻辑在 `PUBLIC_VERIFY_CAPTCHA_MODE = "turnstile" if keys else "math"`。

### 8.4 安装代码

```bash
# 方式 1：git clone（推荐）
ssh root@<VPS_IP>
apt-get update && apt-get install -y git
git clone https://github.com/zmdcyp/legal-notice-gen.git /root/src
cd /root/src

# 方式 2：scp 私有代码
# 本地：rsync -av --exclude=uploads --exclude=venv . root@<VPS>:/root/src/
```

### 8.5 跑一键脚本 + 补环境变量

```bash
sudo bash deploy/setup.sh
# 脚本交互时输入访问密码；或 export LEGAL_NOTICE_PASSWORD=... 再跑
```

脚本建好 systemd unit 后，**手动补上新的环境变量**：

```bash
sudo systemctl edit legal_notice_gen
# 编辑器里加 override 段（无须改原文件）：
```

```ini
[Service]
Environment="LEGAL_NOTICE_PASSWORD=<你的 32 位强密码>"
Environment="VERIFY_BASE_URL=https://v.sspak.law/public/verify"
Environment="TURNSTILE_SITE_KEY=0x4AAAA...存你的 site key"
Environment="TURNSTILE_SECRET_KEY=0x4AAAA...存你的 secret key"
Environment="PUBLIC_VERIFY_ENABLED=1"
# 可选：每次调完 override 运行下面两条
# systemctl daemon-reload && systemctl restart legal_notice_gen
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart legal_notice_gen
sudo systemctl status legal_notice_gen
journalctl -u legal_notice_gen -n 30 --no-pager
# 启动日志里应看到：
#   Public verify: ENABLED at /public/verify (Cloudflare Turnstile)
#   (或 math CAPTCHA fallback 如果 Turnstile 没配)
```

### 8.6 覆盖 Nginx 配置

脚本默认只生成 HTTP + 一个通配 location。按 §7.3 把 Nginx 配置替换成双域名 + 两个 rate limit zone + proxy headers 分文件。

```bash
sudo vim /etc/nginx/nginx.conf
# 在 http {} 里加两条 limit_req_zone

sudo vim /etc/nginx/proxy_headers.conf      # 新建，内容见 §7.3
sudo vim /etc/nginx/sites-available/legal_notice_gen  # 改成 §7.3 的 server block
sudo nginx -t && sudo systemctl reload nginx
```

### 8.7 防火墙 + fail2ban

```bash
# ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable

# fail2ban：封 SSH + Nginx 登录 429 扫描
sudo apt-get install -y fail2ban
sudo cat > /etc/fail2ban/jail.local <<'EOF'
[sshd]
enabled = true
maxretry = 3
findtime = 10m
bantime = 1h

[nginx-req-limit]
enabled = true
filter = nginx-req-limit
port = http,https
logpath = /var/log/nginx/error.log
maxretry = 10
findtime = 10m
bantime = 1h
EOF
sudo cat > /etc/fail2ban/filter.d/nginx-req-limit.conf <<'EOF'
[Definition]
failregex = limiting requests, excess:.* by zone.*, client: <HOST>
EOF
sudo systemctl restart fail2ban
```

### 8.8 日志轮转

```bash
# journalctl 已按 systemd 默认 4 周；调大点以便取证
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cat > /etc/systemd/journald.conf.d/legal-notice.conf <<'EOF'
[Journal]
SystemMaxUse=2G
MaxRetentionSec=12week
EOF
sudo systemctl restart systemd-journald

# Nginx access/error log 用 logrotate
# /etc/logrotate.d/nginx 默认就有；检查保留 14 天
```

### 8.9 备份与恢复

关键数据在 `uploads/`，全部文件级 SQLite + 普通目录——**rsync 到外部存储是最简路径**。

```bash
# /etc/cron.d/legal-notice-backup
0 3 * * *  root  rsync -az \
    /opt/legal_notice_gen/uploads/ \
    backup-user@backup.example.com:/backups/legal_notice/$(date +\%F)/
    && find /backups/legal_notice -maxdepth 1 -type d -mtime +30 -exec rm -rf {} \;
```

更专业点用 SQLite 的 `.backup` 命令拿一致性快照（避免 rsync 走到写入中途的 WAL 文件）：

```bash
sqlite3 /opt/legal_notice_gen/uploads/cases.db \
  ".backup /tmp/cases-$(date +%F).db"
sqlite3 /opt/legal_notice_gen/uploads/history.db \
  ".backup /tmp/history-$(date +%F).db"
# 然后 rsync tmp 里的一致性快照
```

**恢复**：
```bash
sudo systemctl stop legal_notice_gen
sudo cp /backups/.../cases.db    /opt/legal_notice_gen/uploads/cases.db
sudo cp /backups/.../history.db  /opt/legal_notice_gen/uploads/history.db
sudo chown legalnotice:legalnotice /opt/legal_notice_gen/uploads/*.db
sudo systemctl start legal_notice_gen
```

### 8.10 升级与回滚

```bash
# 升级（git clone 部署）
cd /root/src && git fetch && git log HEAD..origin/main --oneline   # 预览
git pull

# 把新代码同步到 APP_DIR（不碰 uploads）
sudo rsync -av --exclude=uploads --exclude=venv --exclude=.git \
    /root/src/ /opt/legal_notice_gen/
sudo chown -R legalnotice:legalnotice /opt/legal_notice_gen

# 如果 requirements.txt 有变化
sudo -u legalnotice /opt/legal_notice_gen/venv/bin/pip install \
    -r /opt/legal_notice_gen/requirements.txt --upgrade

sudo systemctl restart legal_notice_gen
sudo systemctl status legal_notice_gen   # 确认 Active: running

# 回滚：
cd /root/src && git reset --hard <老 commit>
# 再 rsync 一次
```

---

## 9 · 配置参考

### 9.1 环境变量全表

| 变量 | 默认 | 作用 |
|---|---|---|
| `LEGAL_NOTICE_PASSWORD` | 代码默认值（32 位随机） | 工作台登录密码 |
| `VERIFY_BASE_URL` | `https://v.sspak.law/public/verify` | QR 深链 base + PDF 上印的域名（派生 `v.sspak.law`） |
| `PUBLIC_VERIFY_ENABLED` | `1` | `0` 则 `/public/verify` 三条路径返 404 |
| `TURNSTILE_SITE_KEY` | 空 | Cloudflare Turnstile 前端 site key |
| `TURNSTILE_SECRET_KEY` | 空 | Turnstile 后端 secret；`*_KEY` 有其一为空 → 自动切数学题 |

### 9.2 代码级可调（改完要重启）

| 位置 | 默认 | 作用 |
|---|---|---|
| `PUBLIC_VERIFY_RL_PER_MIN` | 5 | 公开验真每分钟配额 |
| `PUBLIC_VERIFY_RL_PER_DAY` | 100 | 公开验真每天配额 |
| `HISTORY_MAX_ROWS` | 2_000_000 | `notice_history` FIFO 上限 |
| `VERIFY_LOG_MAX_ROWS` | 500_000 | `verify_log` FIFO 上限 |
| `TEMPLATES_LOCKED` | `True` | `False` 重新开放模板 CRUD（新建/删除/改名） |
| `MACHINE_PROFILES` | vps=1, mac=min(CPU,6) | 加 `vps-4g: 2` 之类的中档 |

### 9.3 Gunicorn 参数（`setup.sh` 默认）

| 参数 | 值 | 理由 |
|---|---|---|
| `--workers` | **1** | 必须（见 §2.2） |
| `--threads` | 16 | 真正的并发，够 5–10 并发用户 |
| `--worker-class` | `gthread` | 线程模式，不要 gevent |
| `--timeout` | 1800 | 30 分钟兜底（批量任务最长允许值） |
| `--graceful-timeout` | 60 | 重启时的宽限期 |
| `--bind` | `127.0.0.1:5002` | 只监听 loopback，Nginx 反代 |

---

## 10 · 规模极限与扩展路径

### 当前瓶颈

| 瓶颈 | 触发点 | 表现 |
|---|---|---|
| Chromium 内存 | 同时渲染 > 6 个 | OOM kill 或 systemd 反复重启 |
| SQLite 单写 | 多 writer 并发 | "database is locked" 偶发（WAL 下概率低） |
| `TASKS` dict 进程内存 | 单机 | 实例重启丢所有进行中任务的状态 |
| 磁盘 IO | 几十万条 Inventory 导入时 | commit 间歇性慢 |
| 单点故障 | 任一组件挂 | 全服不可用 |

### 扩展路径（按代价排序）

| 改造 | 工作量 | 解什么 |
|---|---|---|
| VPS 垂直升级（4→8 core, 16 GB RAM） | 零 | 并发上限翻倍 |
| Machine Profile 加 `vps-8g: 3` | 20 分钟 | 上面吃满后再加 worker |
| Redis 替换 `TASKS` dict | 1–2 天 | 支持 `--workers N` + 实例重启不丢任务 |
| Postgres 替换 SQLite | 2–3 天 | 真正多 writer 并发 |
| Celery 队列 + Redis broker | 3–5 天 | 任务跨机器分布 |
| 多 VPS + 负载均衡 + 共享 Postgres | 1–2 周 | 水平扩展（真正高可用需要配合之上全套改造） |
| Docker 化 | 1 天 | 迁移 / 回滚 / 多实例部署便利 |
| Kubernetes | 3–5 天 | 只有真正上多节点才值得 |

在"日发 5 000 – 10 000 条 + 存量 40 万"这个量级内，**单 VPS 4 vCPU / 8 GB RAM 足够**，上面所有横向扩展都是过度工程。真需要扩时先从 Redis 替换 TASKS 和 Postgres 替换 SQLite 这两步入手，收益最大、侵入最小。

---

## 参考

- 操作手册：[`DEPLOY.md`](DEPLOY.md)（命令 / 排障 / 卸载）
- 功能总览：[`readme.md`](readme.md)
- 查询 API 规范：[`INQUIRY_API.md`](INQUIRY_API.md)（对外接入）
- 防伪机制：[`ANTI_COUNTERFEIT.md`](ANTI_COUNTERFEIT.md)
- 更新日志：[`CHANGELOG.md`](CHANGELOG.md)
