# 更新日志

本文件记录项目的重要变更。格式：按日期倒序，最新的变更在最上面。

## 2026-04-16 (品牌刷新)

这一轮是视觉层的大改：换新 S&S 盾牌 logo、换 Muhammad Junaid Abbasi 的
签名图（签名 + 姓名 + 职位烘焙成一张图），同步重排签名行，让左右两个章
视觉对称。

### 新增

- **新 logo**（`templates/static/images/logo.png`）
  - 来源：黑底金色盾牌 + 天平 + 橄榄枝 + "S&S" 字样的设计稿
  - **Pillow 后处理脚本**：扫非黑像素的 bounding box → 裁出金色部分 →
    缩到占画布 65% → 居中贴回 1000×1000 黑底画布。保证圆形裁剪时金色
    不顶边，四周有自然留白
  - `.logo-wrap` CSS：`border-radius: 50%` + `overflow: hidden`，
    去掉了原来的 1px 黑边 + 白底（新 logo 自带黑底）
  - 默认 size 从 18mm → **22mm**，与律所章视觉对等（`DEFAULT_ASSETS_CONFIG.logo.size`
    和 JS fallback 同步）

- **新 signature_seal**（`signature_seal_default.png`）
  - 用户上传的 Muhammad Junaid Abbasi 签名图：**手写签名 + 印刷体
    姓名 + "Advocate High Court / (Managing Partner)" 全部烘焙进一张
    1000×1000 PNG**
  - 以前"打印版合伙人名 + 职位文字块 + 蓝色签名章 img 绝对定位叠加"
    的三段组合，合成一张图，简洁得多

- **左右章视觉对称**
  - `.firm-seal-box` 的 `justify-self` 从 `center` 改为 `start`，
    再加 `margin-left: 8mm`，使红色律所章 + "Office Seal" caption 贴左
  - `.stamp-block` 保持 `justify-self: end`（贴右）
  - 两边到各自页边的距离基本相等 → 签名行左右对称

### 变更

- **去掉 `signature-block` 可编辑文本块**（editable blocks 从 **8 → 7**）
  - `EDITABLE_PURPOSES` / `DEFAULT_BLOCKS_TEXT` / `BLOCK_RENDERERS`
    全部移除 `signature-block`；`_render_signature_block` 函数删除
  - 侧栏 Body Content 折叠区少一个 textarea，summary 标签从 "8 editable
    blocks" 改为 "7"
  - 前端 JS `BLOCK_IDS` 常量同步瘦身
  - 签名区现在完全由 `signature_seal` PNG 单张图承担

- **`.stamp-block` CSS 大幅简化**
  - 以前：`position: relative` 容器 + 内部 `partner-name` / `partner-role`
    文字 + 绝对定位叠加的 signature-seal img + `top: 50%; translateY(-50%)` 垂直居中
  - 现在：一个简单的 `display: flex; justify-content: flex-end; align-items: center`
    的盒子，里面只有一张自然流的图（加 transform 旋转）
  - 删除了 `right: 2mm; top: 50%; position: absolute; translateY(-50%); mix-blend-mode: multiply; z-index: 3`
    这一套 overlay hack

- **`signature_seal` 默认尺寸**：54mm → **44mm**
  - 新图是方形（1000×1000），不是之前的 2000×600 横版
  - 44mm 方形 ≈ 原 54mm 横版的占地面积，视觉不突兀

### 文档

- `readme.md`：Body Content 表从 8 行改 7 行；Images 表 `signature_seal`
  建议尺寸从 `2000×600` 改 `1000×1000`，描述改成"签名 + 姓名 + 职位
  一张图"；存储布局注释同步
- `ANTI_COUNTERFEIT.md`：第 5 层（印章）的说明保持不变——实现机制
  （`hash(name)` 种子 + CSS 旋转）没变，只是图本身换了

## 2026-04-16 (晚间)

这一波围绕"工作台体验"和"防伪"做了深度打磨：预览从**保存后刷新**升级为
**边输入边变**；新增**手动单条生成**模式（不需要 Excel）；QR 码重排到
右下角并加上 **16 位防伪序列号**；金额字段自动格式化、Total 自动求和；
文档中文名也能正确下载。

### 新增

- **手动单条生成（Manual single 模式）**
  - Generate 折叠区顶部多了一个 "Excel batch / Manual single" 分段切换
  - Manual 模式下，左栏自动列出当前模板的占位符（按模板里出现的**视觉
    顺序**，不再按字母排序）
  - 每个占位符一个 input；名字含 `date`（大小写不敏感）的自动用日期选择器
  - 后端新增 `GET /api/templates/<slug>/placeholders`（返回 `{placeholders,
    money}`）和 `POST /generate_one`（单条 PDF 流式下载）
  - 前端按 RFC 5987 用 `filename*=UTF-8''...`，中文 / 乌尔都文等非 ASCII
    文件名也能正确下载

- **实时预览（live preview）**
  - `reloadPreview()` 改为 POST 当前 sidebar 状态到新 endpoint
    `/api/preview_html`，浏览器用 `iframe.srcdoc` 渲染——不再依赖"先保存
    再刷新"
  - `wireStaticLivePreview()` + `wireDynamicLivePreview()` 给所有 textarea
    / 滑块 / 颜色 / 图片数字输入挂 300 ms 防抖的 input 监听
  - Manual 模式下还会把已填的 `fields` 一起 POST，后端走 partial-fill
    （填的替换，未填的 `{{foo}}` 保留）

- **防伪增强**
  - QR 码从页 2 签名行的左下位置**移到页面右下角**：距内边框 1.5 cm、
    距页脚分割线 2 cm
  - QR 正下方加**16 位防伪序列号**，格式 `XXXX-XXXX-XXXX-XXXX`，来自
    `hash(name)` 的种子 RNG + 32 字母表（剔除 `0 O 1 I` 以便人眼抄录）
  - 同名复跑相同、不同名必不同——律所可反查真伪
  - `generate_qr_serial(name)` 独立导出，PDF/HTML 渲染和客户端预览共享一条

- **金额字段交互**
  - `MONEY_KEYWORDS` 命中的字段（Principal_Amount / Interest / Penalty
    / Payable）blur 时自动千分位 + 2 位小数（`50000` → `50,000.00`；
    `PKR 50,000` → `50,000.00`；非数字保留原值）
  - **`Payable` 只读**，标签显示 `auto · Principal + Interest + Penalty`；
    前 3 项任一 blur 时自动求和

- **模板顺序工具**
  - `extract_template_placeholders` 改为先把 blocks swap 进 base HTML，
    再扫 `{{...}}`——输出顺序和律师函从上到下的阅读顺序完全一致
    （`date → name → cnic → phone → disb_date → Due_date → Principal →
    Interest → Penalty → Payable → Transaction_id → easypaisa_account`）

### 变更

- **预览默认显示模板**（不再用样本数据）：`/preview.html` 和
  `/api/preview_html` 默认 `fill_placeholders=False`，iframe 里直接看到
  `{{name}}` / `{{cnic}}` 等占位符；水印 / QR / 序列号同样带 `{name}` 字面
  量。传 `?fill=1` 或 POST 带 `fields` 可切回填充
- **`build_notice_html` 拆分 `fill_placeholders` / `strip_unfilled` 两个
  开关**
  - 真实渲染：填充 + 剥除未填 → 干净 PDF
  - 模板预览：不填充 → 全部字面量
  - Manual 实时预览：填充 + 不剥除 → 填的替换，未填保留字面量
- **builtin 模板禁止删除**：Delete 按钮在选中 `default` 时直接
  `display:none`（之前只是 `disabled`，但 `.btn-danger` 没有 disabled
  样式，视觉上还像是可点）

### 修复

- **中文名下载卡住**：非 ASCII 字符写进 `Content-Disposition` 让
  werkzeug 在 `latin-1` 编码时炸 `UnicodeEncodeError`，请求挂住前端一直
  转圈。改用 RFC 5987 双 filename（ASCII 兜底 + UTF-8 percent-encoded）
- **前端下载链 `\w` 删掉中文**：改为只剥文件系统不安全字符
  （`\ / : * ? " < > |`），保留 Unicode 字符

### 测试验证

- `generate_qr_serial('Ali Hassan') == generate_qr_serial('Ali Hassan')` ✓
  不同名输出完全不同 ✓
- POST `/api/preview_html` 带 `fields={name, 4 个金额}` → 已填值替换、
  未填保留 `{{foo}}` ✓
- 中文姓名端到端：`/generate_one` 返回 200 + 正确的 UTF-8 filename\* 头 ✓
- Manual 模式切到 Excel 模式再切回来，之前填的值通过 `prev` 缓存还原 ✓

## 2026-04-16 (下午)

把"可编辑区域"从正文 4 块扩到 **8 块**（覆盖页眉/页脚/签名），并给三张图
加上 size / offset / rotation 的 per-template 控制。未填占位符也统一兜底
为空字符串而不是再把 `{{foo}}` 泄到 PDF 里。

### 新增

- **4 个新的 `data-purpose` 可编辑块**（累计 8 个）
  - `letterhead-firm`    — 律所名（行 1）+ slogan（行 2+）
  - `letterhead-partners`— 每行一个合伙人：`Name | Role`
  - `signature-block`    — 右下签名栏：律师名（行 1）+ 职位（行 2+）
  - `page-footer`        — 底部 office / email / phone，labels 自动加粗
  - 每个块走同一套 `BLOCK_RENDERERS` + `FORMAT_RULES` 管线，用户依然只
    输入纯文本，格式由后端维持

- **`_swap_block_inner` 支持多次替换**
  - 原来首次命中后就返回；改为循环替换所有命中
  - 让 `page-footer` 一次编辑同步到两页

- **三张图片的 per-template 放置参数**
  - `size`（mm）/ `dx` / `dy`（mm）/ `rot`（°）
  - 存在 `uploads/templates/<slug>/assets_config.json`
  - 渲染时以 CSS vars 形式注入到 `.page`，base CSS 里所有 transform 都
    改成 `translate(var()) rotate(calc(var-base + var-random))`
  - `seal` / `signature_seal` 的每文档随机旋转叠加在用户 base 角度之上；
    `logo` 无随机抖动，直接用用户角度
  - 默认值复现原来的基线（logo 18mm / seal 22mm / signature_seal 54mm，
    偏移 0 / 旋转 0）
  - UI 每个图片槽新增 4 个 number input（size / rot / dx / dy），范围：
    logo 12–30mm, seal 16–36mm, sig 40–80mm, rotation ±30°

- **未填占位符 fallback**
  - `build_notice_html` 在显式替换之后多做一步 `PLACEHOLDER_RE.sub("", html)`
  - Excel 和手填都没命中的 `{{foo}}` 统一抹成空串，不会再漏到最终 PDF

### 变更

- **Sidebar · Body Content** 从 4 textarea 扩为 8 textarea
- **Sidebar · Images** 每个槽下面多 4 个数字输入 + 共享一个 "Save placement"
  按钮；"Reset" 拆成"Reset image"（撤销上传）和"Reset placement"（撤销
  数值）
- `save_template` / `api_template_put` / `api_template_create` 新增
  `assets_config` 参数（向后兼容：不传就保留旧值）
- `_template_public` 返回值里带上 `assets_config`

### 测试验证

- 单元级：8 个 block renderer 全部跑通，`build_notice_html` 输出长度、
  `data-purpose` 计数、CSS vars、关键文本全部 OK
- 端到端：`render_notice_row_pdf` 输出 2 页 A4，`get_text()` 返回 0 字符
  （光栅化），`pikepdf.is_encrypted == True`（AES-256 锁）
- 修改 `assets_config.seal.rot = 15 / dx = 2` / `logo.size = 20` 也能
  正确落到 CSS vars 里

## 2026-04-16

这一轮是项目从 DOCX-based 向 **HTML-based 全栈工作台**的重写。
前端从 3 步向导合并为单页工作台；生成流水线从 docx + soffice 换成
Chromium + 光栅化；所有模板/图片/水印参数都可在网页里编辑。

### 新增

- **单页工作台 `/`**（取代旧 3-step 上传向导）
  - 420 px 左侧边栏 × 全高右侧 iframe 实时预览
  - 左栏 5 个折叠区：Template / Body Content / Images / Watermark & Pattern / Generate
  - 任何保存动作都触发 iframe 120 ms 防抖刷新

- **HTML 模板架构**（替换原来的 docx 模板）
  - 基础布局 `templates/legal_notice_full.html`（双线边框 / Libre Baskerville
    正文 + Playfair Display 标题 / 网格化 TO: 字段 / 金额隐形表格 /
    合伙人列表 / 双页脚）
  - 4 个 `data-purpose` 可编辑块：`notice-subject` / `notice-body-text` /
    `legal-consequences` / `payment-instructions`
  - 格式托管：用户只输入纯文本，后端按 `FORMAT_RULES` 表自动套
    `<em>` / `<strong>` / `<span>`，保证跨编辑格式一致

- **多模板管理**
  - 内置 `default` 模板，可"另存为新模板"克隆
  - 每个模板有独立的 blocks / security / assets
  - 模板列表下拉、改名、删除、reload 全部在左栏完成

- **3 张可替换图片**
  - `logo`（顶部）/ `seal`（红色律所章）/ `signature_seal`（蓝色签名章）
  - 每张可上传 PNG 覆盖，可一键 Reset 回出厂
  - 用户新建的模板自动继承 base 的图片 copy

- **水印 + 防伪底纹参数化**
  - 水印：启用开关 / 英文+乌尔都语模板 / 字号 10-64 px /
    透明度 0-100% / 数量 1-100 / 墨水色
  - 底纹：启用开关 / 透明度 / 密度（low/medium/high/ultra）
  - 每模板独立 `security.json`

- **防提取 PDF 渲染流水线**
  - Playwright + Chromium → 矢量 PDF
  - PyMuPDF → 每页 300 dpi 光栅化重组（无文字对象）
  - pikepdf → AES-256 权限锁（禁 extract / 禁 modify / 允许 print）+
    元数据（Producer / Title / CreationDate）
  - 每 worker 独立 Chromium 实例，ProcessPoolExecutor 跨多 worker 并行

- **每文档唯一随机化**
  - 水印/底纹/印章旋转都用 `hash(name)` 作 RNG 种子
  - 同名复跑一致，不同名必然不同；防止 copy-paste 伪造

- **模板 CRUD API**
  - `GET/PUT/POST/DELETE /api/templates/<slug>`
  - `POST/DELETE/GET /api/templates/<slug>/assets/<kind>`
  - `GET /preview.html?slug=<slug>` 供 iframe 预览

### 变更

- **默认输出：DOCX → PDF（不可提取）**
  - 整个 docx 路径被替换为 HTML → Chromium → 光栅化
  - DOCX 输出选项移除（工具现在只生成 PDF）

- **依赖更新**
  - 移除：`python-docx`
  - 新增：`playwright`、`pymupdf`、`pikepdf`、`Pillow`、`qrcode`、
    `arabic-reshaper`、`python-bidi`
  - 部署脚本移除 `libreoffice-writer`，新增 `playwright install-deps chromium`
    + `playwright install chromium`

- **Machine Profile 简化**
  - 删掉 docx 专属的 `convert_workers` / `pdf_chunk_size`
  - 只保留 `render_workers`：VPS 1 worker / Mac up to 6 workers
  - 1 worker ≈ 250 MB RAM (Chromium)，VPS 默认保守

- **字体切换**
  - 正文从实验过的 EB Garamond 换回 **Libre Baskerville**（用户偏好）
  - 标题保持 **Playfair Display**
  - 字体文件随包放在 `templates/static/fonts/`，不依赖 CDN

### 删除

- 所有 docx 相关代码（~520 行）：`_replace_in_*`、`_render_one_job`（docx 版本）、
  `extract_placeholders`（docx 版）、`_batch_docx_to_pdf`、`_apply_security_overlay`
  （docx 锚定 seal 的 VML 注入）、`_build_anchor_drawing`、`_add_floating_image`、
  OOXML `_NS_*` 命名空间常量、`SOFFICE_TIMEOUT_*`
- 旧的 `/editor` 独立页（合并进 `/`，保留 302 跳转向后兼容）
- 旧 3-step 主页 HTML_TEMPLATE

### 安全

- **permissions lock**：owner 密码 32 字节随机无人持有；user 密码空串
  （打开无障碍）；权限位禁 copy / 禁 modify
- **text-to-pixel 转换**：PyMuPDF 300 dpi 把整页变一张图，PDF 里没有任何
  文字对象——Adobe/Foxit/Preview 都选不中

### 测试验证

- 3 条样例端到端跑通：2 页 A4、加密、`extract=False`、`modify=False`、
  `print=True`、`get_text()` 返回 0 字符
- 按 `agent` 分组输出：正确分桶、独立 zip、流式下载
- 模板 CRUD：创建 / 改名 / 克隆 / 删除 / 上传图片全通

## 2026-04-11

本次更新聚焦于生产可用性、性能和业务呈现风格，从零散功能迭代升级为面向
5000+ 量级批量生成的完整工具。

### 新增

- **网页日期选择器**
  - 在"生成"页新增 `<input type="date">`，默认值为当天。
  - 只要模板中包含 `{{date}}` 占位符，日期选择器会自动显示，并从"缺失字段"
    的手填列表中隐藏（避免重复）。
  - 无论 Excel 是否存在 `date` 列，网页选择的日期都会 **无条件覆盖** 所有
    行的 `date` 字段；即使 Excel 里写了日期也会被忽略。
  - 用户输入以 ISO（`YYYY-MM-DD`）从前端传来，后端解析后统一输出为
    `DD/MM/YYYY` 格式。

- **统一日期输出格式：`DD/MM/YYYY`**
  - `read_excel()` 对所有 `datetime` / `date` 单元格改为 `%d/%m/%Y` 格式化。
  - 覆盖所有日期列（`dist_date`、`due_date` 以及任何其他日期字段），由单点
    格式化保证整个项目一致。

- **机器配置文件（Machine Profile）运行时选择**
  - 新增 `MACHINE_PROFILES` 配置字典，目前包含两个档位：
    - `vps`（默认）：保守配置，`render_workers=2`、`convert_workers=2`、
      `pdf_chunk_size=15`，适合 2 核小型 VPS。
    - `mac`：全力档位，`render_workers=os.cpu_count()`、
      `convert_workers=os.cpu_count()`、`pdf_chunk_size=10`，在多核本地机
      上释放所有核心。
  - 网页"生成"页新增 **Machine Profile** 下拉框，默认 VPS，用户可在生成前
    切换。选择后 `/generate` 接口通过 `payload.machine` 查找配置，再传入
    `generate_notices()`。
  - 启动时打印所有 profile 及检测到的 CPU 核数，方便排查。

- **ProcessPoolExecutor 渲染阶段（DOCX 性能关键优化）**
  - 将 `render_one` 闭包提取为模块级函数 `_render_one_job`，使其可被
    `ProcessPoolExecutor` pickle。
  - `generate_notices()` 在任务开始时创建一次长生命周期的进程池，跨所有
    分组复用，任务结束在 `finally` 中关闭；避免了每组都付出进程启动开销。
  - `_process_group()` 渲染阶段改用 `submit` + `as_completed`，主进程统
    计完成数做进度更新，免去跨进程共享状态。
  - 进程池创建失败时（`OSError` / `ImportError` / `NotImplementedError`）
    自动回退到 `ThreadPoolExecutor`，保证单文件交付模型在任何环境下都能
    跑；任务消息里会显示实际使用的是 `processes` 还是 `threads`。
  - 影响：DOCX 输出的渲染瓶颈原本是 python-docx 的 GIL；切换到进程池后
    渲染能力随核数近似线性扩展。在 12 核 M2 Max 上预计比线程池快数倍。

- **金额列自动格式化为 `1,234.56` 风格**
  - 新增 `MONEY_KEYWORDS` 关键词表 + `_is_money_header()` 工具函数，基于
    列名识别金额列。
  - 关键词覆盖英文：`amount`、`interest`、`penalty`、`payable`、`fee`、
    `balance`、`total`、`principal`、`charge`、`payment`、`debt`、
    `price`、`cost`；以及中文：`金额`、`利息`、`罚息`、`罚金`、`应付`、
    `应还`、`应缴`、`费用`、`总额`、`本金`、`欠款`、`滞纳金`、`款项`。
  - `read_excel()` 对匹配金额关键词的列统一预格式化为 `f"{v:,.2f}"`，
    无论 Excel 里存的是整数还是浮点数都会得到 `500,000.00` / `1,234.56`
    的正式格式。
  - 明确排除 `_date` 结尾的列（保护 `dist_date`、`due_date` 等日期列
    不被误判为金额）。
  - 典型字段验证：`Principal_Amount` / `Interest` / `Penalty` / `Payable`
    都能正确带逗号和两位小数。

### 变更

- **默认输出格式：`PDF` → `DOCX`**
  - HTML 下拉框将 DOCX 标为 `selected`，`/generate` 接口默认值改为
    `"docx"`，`generate_notices()` 的签名默认值同步改为 `"docx"`。
  - 原因：实际工作流中 DOCX 为主，跳过 soffice 能把 5000 量级压到秒级完成。
    PDF 仍然可选。

- **README 重写**
  - 按"占位符驱动、业务无关"的设计原则重写，删除了带有具体业务字段的臆测
    示例（原先用过的 CNIC / 姓名 / 客户等）。
  - 补充单文件交付说明、VPS 部署与 Gunicorn 单 worker + 多 threads 的原因、
    5000 量级并发实现细节、异步任务 API、已知事项。

- **`deploy/setup.sh` 生产配置对齐**
  - Gunicorn：`--workers 2 --timeout 120` → **`--workers 1 --threads 16
    --worker-class gthread --timeout 1800 --graceful-timeout 60`**。
    原因：任务状态保存在进程内存中的 `TASKS` 字典里，`/status` 和
    `/download` 必须落到同一个进程。多 worker 会让轮询命中其他进程拿不到
    任务。只能用单 worker + 多 threads 做并发。
  - Nginx：`client_max_body_size 50M → 100M`，
    `proxy_read_timeout 120s → 1800s`，新增 `proxy_send_timeout 1800s` 和
    `proxy_buffering off`，让 5000 量级的大批量生成不被代理层中断。

- **`read_excel()` 数值处理逻辑**
  - 新增金额列识别分支（优先级最高）。
  - 整数型浮点数（如 CNIC / 电话号码）继续走 `int(v)` 分支，避免出现
    `1234.0` 尾巴。
  - 其他非整数浮点数走 `_format_value()` 兜底，也会得到 `,.2f` 格式。

### 删除

- 删除早期作为演示用的样例文件与脚本（`create_sample.py`、
  `sample_template.docx`、`sample_data.xlsx`），项目现在完全由用户自己
  提供模板和数据，不再内置任何业务相关示例。

### 修复

- **转换阶段进度条卡顿**
  - 之前把所有 docx 平均分给 `MAX_WORKERS` 个大 batch，每个 worker 要等整
    个 batch 全部转完才上报一次进度，导致 0% 卡很久后"一下子"跳完。
  - 改为固定 `pdf_chunk_size`（VPS 15 / Mac 10），把任务切成很多小块喂给
    线程池，每完成一个 chunk 就 bump 一次进度，UI 平滑推进。

- **按负责人分组输出 & 一边生成一边下载**
  - 按 `group_by_field`（通常是 `agent`）排序后分桶，一个组处理完立即
    打包并放进 `ready_parts` 列表。
  - 前端每秒轮询 `/status`，为每个新增的 `ready_parts` 条目触发一次
    `/download/<task_id>/<part_index>` 下载，不再生成"最外层总包"。
  - 页面底部展示已下载分组的累计列表。

### 技术细节备忘

- **每任务一次进程池启动** —— 在 `generate_notices()` 级别持有 render 池，
  跨所有分组复用；`try/finally` 确保任务异常时也能 `shutdown(wait=True)`，
  不会泄漏 worker 进程。
- **soffice profile 隔离（保留）** —— 每个 worker 用独立的
  `-env:UserInstallation=file:///tmp/soffice_profile_N`，这是让多 soffice
  真正并行的前提。
- **单文件交付原则（保留）** —— 所有 HTML / CSS / JS 继续内联在
  `legal_notice_gen.py` 里，不引入外部模板或静态资源目录。本次所有新功能
  都在这个约束下完成。
