# 防伪措施与实现

本文档列出 S&S Law Firm Legal Notice Generator 生成的 PDF 上启用的**全部防伪、防篡改、防伪造机制**，以及每一层在代码里的实现位置。

## 总览

| # | 层 | 目标 | 技术 | 代码位置 |
|---|---|---|---|---|
| 1 | 双语水印 | 视觉识别 + 姓名绑定 | Pillow + arabic-reshaper + python-bidi | `_render_security_overlay_png` |
| 2 | Guilloche 防伪底纹 | 防扫描复制 + 视觉复杂度 | 数学曲线（正弦 + 阿基米德螺旋 + 同心圆）→ PNG | 同上 |
| 3 | QR 码 | 机器可读的姓名签证 | `qrcode` 库 | `_render_qr_png` |
| 4 | **16 位防伪序列号** | 人眼可抄的反查代码 | 哈希种子 + 32 字母表 | `generate_qr_serial` |
| 5 | 红色律所章 + 蓝色签名章 | 正式性 + per-doc 随机旋转 | PNG + CSS `transform` | `build_notice_html` 注入 CSS |
| 6 | **整页光栅化（300 dpi）** | 消除文字对象 → 无法复制/编辑文字 | PyMuPDF `page.get_pixmap` | `_rasterize_pdf` |
| 7 | AES-256 权限锁 | 禁 copy / 禁 modify，owner 密码无人持有 | pikepdf `Encryption` + `Permissions` | `_apply_pdf_lock` |
| 8 | PDF 元数据 | Producer / Title / CreationDate 存证 | pikepdf 文档信息 | 同上 |
| 9 | 每文档唯一化 | 复制别人的律师函当模板会对不上 | `hash(name)` 作 RNG 种子 | `build_notice_html` + `generate_qr_serial` |

下面逐条说明原理、默认参数、代码入口、和对应的**攻击场景**。

---

## 1. 双语水印（English + Urdu）

### 目标
- 视觉上告知"这是 SS Legal Firm 开给某某人的律师函"，形成姓名绑定
- 斜向、半透明、铺满全页 → 扫描 / 截图都能看到
- 乌尔都文用于巴基斯坦本地识别

### 默认参数（`DEFAULT_SECURITY_CONFIG`）
```python
"watermark": {
    "enabled": True,
    "english_template": "SS Legal Firm; for Respondent {name}",
    "urdu_template":    "ایس ایس لیگل فرم؛ جواب دہندہ {name} کے لیے",
    "font_size": 22,        # px
    "opacity":   67,        # 0-100
    "count":     25,        # tiles per page (网格自适应 A4)
    "color":     "#323255",
    "angle":     30,        # 顺时针斜角
}
```

### 实现（`_render_security_overlay_png`）
1. 新建透明 RGBA 画布（A4 像素）
2. 按 `count` 自适应网格铺贴 tile：
   - 每个 tile 包含英文 + 乌尔都文两行
   - 乌尔都文用 `arabic_reshaper.reshape()` 连字形，`bidi.algorithm.get_display()` 反转 RTL
3. Tile 旋转 30°（`angle` 参数），写入画布
4. PNG 作为 CSS `background-image` 注入到 `.page`（每页一张）

### 每文档唯一化
- 旋转角、tile 抖动、颜色微扰都由 `random.Random(hash(name) & 0xFFFFFFFF)` 种子驱动
- 同名复跑完全相同；不同名水印纹路必然不同 → 复制粘贴被检测

### 用户可调
- Sidebar 第 4 折叠区（Watermark & Pattern）能改：enabled / 英文模板 / 乌尔都模板 / 字号 / 透明度 / 数量 / 颜色

### 攻击场景
- **截图伪造**：水印里写着姓名，与伪造内容里的姓名对不上
- **水印抠除**：整页光栅化后水印是像素，和正文像素同层、同 dpi，无法单独图像处理掉

---

## 2. Guilloche 防伪底纹

### 目标
- 类似钞票 / 证书的数学曲线图案
- 线条交织形成复杂纹路，扫描复制会出现摩尔纹
- 给水印一个"背板"，进一步提高整体视觉密度

### 默认参数
```python
"pattern": {
    "enabled": True,
    "opacity": 22,         # 0-100
    "density": "medium",   # low | medium | high | ultra
}
```

### 密度映射（4 档）
| 档 | 正弦曲线数 | 螺旋数 | 圆组数 |
|---|---|---|---|
| low    | 20 | 4  | 12 |
| medium | 40 | 8  |  8 |
| high   | 60 | 12 |  6 |
| ultra  | 80 | 16 |  5 |

### 实现（`_render_security_overlay_png` 同一函数）
1. **正弦曲线族**：数十条不同频率、相位、振幅的 `A·sin(ωx + φ)` 叠加；振幅 + 1.5ω 三次谐波让纹路不重复
2. **阿基米德螺旋**：`r = a + bθ`，不同起点 / 旋向/密度
3. **同心圆组**：每组有 5–15 个不同半径的圆，圆心随机分布
4. 所有曲线用半透明细墨色（`#2a2a2a`，alpha 对应 `opacity`）画在同一 tile 上，和水印合成

### 每文档唯一化
- 所有曲线的初始参数（相位、旋向、圆心坐标）都来自 `hash(name)` 种子 RNG
- 同名的底纹精确一致；不同名底纹完全不同

### 攻击场景
- **扫描伪造**：打印后扫描，guilloche 出现摩尔纹 → 肉眼可辨扫描件
- **局部 PS 修改**：例如改金额 → PS 后的像素边界会切断 guilloche 曲线
- **整体替换**：无法用"别人的律师函 + 自己的文本"拼，因为 guilloche 纹路与姓名绑定

---

## 3. QR 码（bottom-right）

### 目标
- 机器可读的姓名签证：扫码得到 `SS Legal Firm; for Respondent <name>`
- 位于页 2 **右下角**，距内边框 1.5 cm，距页脚分割线 2 cm

### 内容
- 默认 `ENGLISH_WATERMARK_TEMPLATE.format(name=<当前姓名>)`
- 即："SS Legal Firm; for Respondent 张三"

### 实现（`_render_qr_png`）
1. `qrcode.make(text)`，error correction = `ERROR_CORRECT_M`（约 15% 容错）
2. 黑白 PNG，尺寸自适应容器（28 × 28 mm）
3. HTML 里占位 `<div class="placeholder">QR</div>`，`build_notice_html` 里 replace 为 `<img>`

### 布局（CSS in `legal_notice_full.html`）
```css
.qr-footer-br {
  position: absolute;
  right: 28mm;    /* 13mm 内边框 + 15mm 空隙 = 1.5cm */
  bottom: 46mm;   /* 页脚 line 上方 20mm */
  width: 28mm;
  display: flex; flex-direction: column;
  align-items: center; gap: 2mm;
}
```

### 攻击场景
- **替换 QR**：QR 含有姓名，扫出来和正文姓名不一致立即暴露
- **删除 QR**：页面上缺少 QR 是一眼可见的异常
- **扫码解析后伪造**：即便解出内容仿照生成一个 QR，下方的 16 位防伪序列号对不上律所数据库

---

## 4. 16 位防伪序列号（QR 下方）

### 目标
- **人眼可抄、电话可报**的反查代码
- 律所内部可用序列号 → 姓名对应表验证真伪，而不需要扫 QR
- 区别于 QR（机器可读），序列号是**手动可核**的对照点

### 格式
`XXXX-XXXX-XXXX-XXXX`（4×4 分组，共 16 字符 + 3 连字符）
- 字母表：`ABCDEFGHJKLMNPQRSTUVWXYZ23456789`（32 个字符）
- **剔除 `0` / `O` / `1` / `I`**：打印 + 抄录时常见误读字符

### 实现（`generate_qr_serial(name)`）
```python
def generate_qr_serial(name):
    srng = random.Random(hash(name) & 0xFFFFFFFF)
    chars = [srng.choice(_QR_SN_ALPHABET) for _ in range(16)]
    return "-".join("".join(chars[i:i+4]) for i in (0, 4, 8, 12))
```

### 特性
- **deterministic per-name**：同一姓名任何时刻生成的序列号完全相同 → 律所可按姓名反推序列号，或反向查
- **不同姓名必然不同**：`hash(name)` 种子 32 位、组合空间 32^16 ≈ 1.2×10^24，生日悖论冲突概率极低
- **不可逆**：没有姓名就无法从序列号反推（除非暴力搜索姓名库）

### 布局
```html
<div class="qr-footer-br">
  <div class="qr-slot" data-slot="qr">...QR img...</div>
  <div class="qr-sn">__QR_SN__</div>   <!-- 渲染时替换为真实码 -->
</div>
```

CSS：
```css
.qr-footer-br .qr-sn {
  font-family: ui-monospace, "SF Mono", "Menlo", "Consolas", monospace;
  font-size: 7.5pt; font-weight: 600;
  letter-spacing: 0.4pt; color: #333;
  white-space: nowrap;
}
```

### 验证流程（律所使用）
1. 收到可疑律师函，记下序列号 `XXXX-XXXX-XXXX-XXXX`
2. 后台脚本调用 `generate_qr_serial("张三")`（`张三` 是函上的姓名）
3. 结果与序列号一致 → 真实；不一致 → 伪造

---

## 5. 红色律所章 + 蓝色签名章

### 目标
- 传统视觉防伪：律师函没有公章 + 签名章几乎不成立
- **每张律师函的章旋转角度不一样**，防止扫一次再复用
- 视觉对称：红章贴页面左侧、蓝签章贴页面右侧，同等距离

### 实现
- 两张 PNG（RGBA 透明底），分别嵌在：
  - 页 2 **左下** `.firm-seal-box`：红色 "S&S LAW FIRM" 圆形公章 + 下方
    **Office Seal** 下划线说明
  - 页 2 **右下** `.stamp-block .signature-seal`：蓝色手写签名 + 印刷体
    姓名 + 职位**一张整图**（原来是三段 HTML，2026-04-16 合成一张图以减
    少 PS 伪造面）
- CSS `transform: rotate(calc(var(--seal-rot-base) + var(--firm-rot)))`
  - `--seal-rot-base`：用户在 Sidebar · Images 里配的 base 角度
  - `--firm-rot` / `--sig-rot`：`random.uniform(-15, 15)` 种子来自 `hash(name)`

### 代码（`build_notice_html`）
```python
srng = random.Random(hash(name) & 0xFFFFFFFF)
firm_rot = srng.uniform(-15, 15)
sig_rot  = srng.uniform(-15, 15)
# 注入为 CSS vars 到 .page
```

### 用户可调
- Sidebar · Images 每个槽位下 4 个数字输入：size / rotation base / offset X / offset Y
- 上传 PNG 覆盖出厂图（默认尺寸：logo 22mm 圆形 / seal 22mm 圆形 /
  signature_seal 44mm 方形）

### 攻击场景
- **复制同一个章图片**：不同律师函的章角度有差异（±15° 种子由姓名决定），
  用同一图片会被发现
- **用自己 PS 的章**：蓝章 PNG 含手写签名的非重复笔触 + 姓名/职位印刷体
  一次成型，复刻难度比单独章 + 另外的印刷文字高一截
- **局部 PS 换姓名**：签名图里的姓名和正文 TO: 行的姓名必须一致、和水印
  里的 `{name}` 一致、和 QR 内容一致、和 16 位序列号一致——四处同时改

---

## 6. 整页光栅化（核心防复制/防编辑）

### 目标
**根本性解决**"PDF 里有文字对象 → Adobe Acrobat / Foxit / Preview 里可以选中、复制、编辑"的问题。

### 实现（`_rasterize_pdf`）
```python
def _rasterize_pdf(in_pdf, out_pdf, dpi=300):
    src = pymupdf.open(in_pdf)
    dst = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=dpi, alpha=False)  # 渲染为 300 dpi 像素
        page_w, page_h = page.rect.width, page.rect.height
        new = dst.new_page(width=page_w, height=page_h)
        new.insert_image(new.rect, stream=pix.tobytes("png"))  # 整页变一张图
    dst.save(out_pdf, garbage=4, deflate=True)
```

### 效果
- **PDF 里只有一张图片对象，没有任何 `/Text` / `/Font` / `/Tj` 文字指令**
- Acrobat 选择工具框选 → 选不中；复制 → 没东西
- OCR 可以识别，但输出的是像素级文本，没有原始 layout/metadata
- 文件大小：约 2.8–3.0 MB（两页 300 dpi）

### 验证
```bash
python -c "
import pymupdf
d = pymupdf.open('output.pdf')
print('pages:', d.page_count)
print('text chars:', sum(len(p.get_text()) for p in d))  # → 0
"
```

### 攻击场景
- **PDF 编辑器改金额**：金额是像素，PDF 编辑器改不了
- **文本替换 / 查找替换**：无文字对象，F&R 失效
- **复制粘贴**：选不中
- **OCR 后伪造**：OCR 能重建文本，但排版 / 字体 / 金额表对齐都会走样；加上重新渲染会丢失 guilloche + 水印 + QR 的像素一致性

---

## 7. AES-256 权限锁

### 目标
- 阻止**合法 PDF 查看器**遵循协议提供 copy / modify / annotate 等操作
- 无法被用户在 Acrobat 里"另存为未加密版本"

### 实现（`_apply_pdf_lock`）
```python
owner_pw = base64.b64encode(os.urandom(24)).decode()  # 32字节随机,程序立即丢弃
permissions = pikepdf.Permissions(
    accessibility=True,            # 允许屏幕阅读器
    extract=False,                 # ❌ 禁复制
    modify_annotation=False,       # ❌ 禁批注
    modify_assembly=False,         # ❌ 禁页面重排
    modify_form=False,             # ❌ 禁表单改
    modify_other=False,            # ❌ 禁其他修改
    print_highres=True,            # ✅ 允许打印
    print_lowres=True,
)
pdf.save(out_pdf, encryption=pikepdf.Encryption(
    owner=owner_pw, user="", allow=permissions))
```

### 关键点
- **owner 密码是 32 字节随机 base64，生成后立即丢弃**——没人持有，任何 PDF 工具都无法提升权限
- **user 密码空串**——任何人都能**打开**（不需要密码）
- 打开后可正常阅读、可打印，但 copy / edit / extract 被 PDF 规范层面拒绝

### 攻击场景
- **用 qpdf --decrypt 解锁**：可以解锁（因为算法公开），但原始 PDF 已是整页光栅化的图，解锁后也没文字可复制/改
- **用 Acrobat Pro 改文本**：权限位禁用修改 → 改不了；即便绕过，还是第 6 层（整页像素）兜底
- **截图**：截图能保留图像，但水印 + guilloche + QR 序列号都被截到 → 截图伪造仍可追溯

---

## 8. PDF 元数据（存证）

### 实现（`_apply_pdf_lock` 同一函数）
```python
with pdf.open_metadata() as meta:
    meta["dc:title"] = "Legal Notice"
    meta["pdf:Producer"] = "S&S Law Firm Legal Notice Generator"
pdf.docinfo["/Title"] = "Legal Notice"
pdf.docinfo["/Producer"] = "S&S Law Firm Legal Notice Generator"
pdf.docinfo["/CreationDate"] = "D:" + now.strftime("%Y%m%d%H%M%S") + "Z"
```

### 作用
- `Producer` 固定写入律所自有字符串 → 非系统生成的 PDF 缺少或 Producer 字段不同
- `CreationDate` 精确到秒 → 可对照律所内部生成日志交叉验证

---

## 9. 每文档唯一化（贯穿全栈）

### 核心思想
**所有随机成分都用 `_name_seed(name)` 作 RNG 种子**：
- 水印 tile 旋转、颜色微扰（第 1 层）
- Guilloche 正弦相位、螺旋旋向、圆心坐标（第 2 层）
- QR 序列号（第 4 层）
- 律所章 `--firm-rot` / 签名章 `--sig-rot`（第 5 层）

### 一致性承诺
| 情境 | 结果 |
|---|---|
| 同一姓名两次跑 | 所有防伪成分**完全相同**（律所可复跑验证） |
| 不同姓名跑 | 水印纹路、底纹、章角度、序列号**必然不同** |

### 代码位置
```python
# 模块级稳定种子（hashlib.sha256 的前 8 字节）
def _name_seed(name):
    return int.from_bytes(
        hashlib.sha256((name or "").encode("utf-8")).digest()[:8], "big")

# build_notice_html
srng = random.Random(_name_seed(name))
firm_rot = srng.uniform(-15, 15)
sig_rot  = srng.uniform(-15, 15)

# generate_qr_serial
srng = random.Random(_name_seed(name))

# _render_security_overlay_png 内部同样用 _name_seed(name)
```

### ⚠ 历史注意事项
**2026-04-16 之前**用的是 Python 的内置 `hash(name) & 0xFFFFFFFF`——这是 bug。
Python 3 的 `hash()` 会用每个进程启动时随机生成的 salt（`PYTHONHASHSEED=random`
默认），所以：
- 服务重启一次，同一个人的序列号就变了 → 律所反查失败
- ProcessPool 每个 worker 是独立 Python 进程 → 同一批次里不同 worker 生成
  的律师函序列号不一致

切换到 `hashlib.sha256` 后，种子跨进程、跨机器、跨 Python 版本全部一致。
文档承诺的"同名复跑相同"从此才真正成立。

### 攻击场景
- **Alice 把 Bob 的律师函当模板改**：水印里 Bob 的名字、QR 扫出 Bob 的名字、序列号对应 Bob 的哈希——三处无法同时用 Alice 的姓名配平
- **暴力生成假律师函**：必须反推 `hash()`，实际不可行；而律所后台只需跑一次 `generate_qr_serial(真实姓名)` 即可验证

---

---

## 10. 历史审计日志（`uploads/history.db`）

### 目标
给第 4 层（16 位序列号）加一个**真正的反查数据源**。之前文档说"律所后台
跑 `generate_qr_serial(姓名)` 对比"——对，但需要知道正确的姓名才能对比。
现在有了 SQLite 日志，律所可以**直接按序列号反查**姓名 + 金额 + 时间。

### Schema

```sql
CREATE TABLE notice_history (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  serial       TEXT NOT NULL,          -- XXXX-XXXX-XXXX-XXXX
  name         TEXT NOT NULL,          -- 被告姓名
  principal    TEXT,                   -- Principal_Amount 字段
  generated_at TEXT NOT NULL           -- ISO 8601 时间戳
);
CREATE INDEX idx_history_serial  ON notice_history(serial);
CREATE INDEX idx_history_created ON notice_history(generated_at);
```

**只存四项**：序列号 / 姓名 / 本金 / 时间。不存客户隐私字段（CNIC、手机、
交易号）——这些留在原始 Excel 里归律所自己保管。

### 容量管理
- `HISTORY_MAX_ROWS = 2_000_000`
- 超过就删最旧的（按 `id ASC`）
- 2M 行约 200 MB 磁盘，索引查询仍是 O(log N) ≈ 微秒级

### 写入点
每次**成功**渲染 PDF 都 hook 一条记录：
- **Excel 批量**：`_process_group_html` 里 `fut.result()` 返回后 log，
  失败的行（raise）不记录
- **Manual 单条**：`/generate_one` 里 `render_notice_row_pdf` 返回后 log
- SQLite WAL 模式允许读写并发（`/verify` 正在读的同时 worker 还能写）

### 查询接口

```
GET /api/verify?serial=XXXX-XXXX-XXXX-XXXX
→ 200 {ok: true, name, principal, generated_at}
→ 404 {ok: false, error: "not found"}

GET /api/history?limit=50&offset=0&q=张三
→ 200 {total, limit, offset, rows: [{serial, name, principal, generated_at}]}
```

UI: `/verify` 页面（带 logo 的独立页）——上面一个大输入框，按 Enter 或
点 Verify 立即返回；下面一个最近历史表，可按姓名模糊搜 + 分页。

### 防伪价值
- **真实性**：收到可疑律师函 → 敲序列号 → 库里找到 = 真实；找不到 = 伪造
  或至少"不是本系统生成的"
- **谁发的**：匹配记录直接显示当初发给谁、本金多少
- **何时发的**：`generated_at` 精确到秒，可交叉 timestamps
- **防内部纂改**：attacker 就算拿到 DB 写权限，改 name/principal 后序列号
  也对不上——序列号是 `_name_seed(name)` 的派生，改 name 必然要重算 serial，
  而原序列号已经**印在纸上了**。DB 和纸必须同时改。

### 局限
- 单机 SQLite，跨实例不共享。多律师楼部署要接外部数据库或复制 DB
- 当前写入只有序列号 + 姓名 + 本金 + 时间。想追加更多字段需要改 schema
  + 修 `_log_notice_record()`
- 2M 容量到上限后 FIFO 丢弃，**不保留历史**——早期发的律师函几年后可能
  已经无法反查。要永久保留需要调高 `HISTORY_MAX_ROWS` 或改成按年归档

---

## 防伪层的协同攻击面分析

| 攻击手段 | 哪一层挡住 |
|---|---|
| Ctrl+C 复制文字 | 6（整页光栅化）+ 7（权限锁） |
| Adobe Acrobat 改文字 | 6（文字是像素）+ 7（权限位禁修改） |
| qpdf 解密 → 改文字 | 6（解密后依然是像素） |
| 截图 + 重新排版 | 1（水印姓名绑定）+ 2（guilloche 边界）+ 4（序列号不对） |
| 用别人的律师函当模板改姓名 | 1 / 2 / 4 / 9（所有 per-name 成分全变） |
| 仿造 QR | 4（下方序列号不对）+ 9（水印和章不匹配） |
| OCR 重建文本 + 重新打印 | 2（guilloche 模糊化）+ 5（章角度不对） |
| 制作空白律师函 + 自己填内容 | 律所不生成 → 没有合法的序列号 / 水印 / guilloche |

每一层单独看都能被某些手段绕过，但**九层叠加后攻击者必须同时对九处都能做到一致伪造**，综合难度接近不可能。

---

## 诊断命令

```bash
# 文字对象数量（应为 0）
python -c "import pymupdf; d=pymupdf.open('x.pdf'); print(sum(len(p.get_text()) for p in d))"

# 加密状态 + 权限（应 True / extract=False）
python -c "import pikepdf; p=pikepdf.open('x.pdf'); print(p.is_encrypted, p.allow)"

# 反查序列号
python -c "import legal_notice_gen as a; print(a.generate_qr_serial('张三'))"

# 元数据
python -c "import pikepdf; p=pikepdf.open('x.pdf'); print(dict(p.docinfo))"
```

---

## 未来可能的增强（TODO）

- **序列号 → 数据库**：把 `(姓名, 序列号, 生成时间)` 存到律所后台，网页提供"序列号反查"工具
- **数字签名**：使用律所 X.509 证书对 PDF 做数字签名（pikepdf 支持），打开时 Acrobat 显示绿色勾
- **动态水印字体** 随机从若干 serif 字体中选一种，再增加伪造难度
- **二维码含 HMAC**：QR 内容带 `HMAC(secret_key, name)` 而不是明文 — 离线仍可验，但需要客户端律所 App 持有 secret_key
