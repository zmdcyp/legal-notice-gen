# Inquiry API

只读查询接口，用于**验证律师函真伪**和**浏览生成历史**。未来可能被第三方系统接入（律所内网的 OA、移动端 App、合作伙伴的核验服务等），所以这份文档独立维护。

- **Base URL**：`https://<your-host>/api/`
- **认证**：`Flask session` cookie（打 `/login` 拿到 `session`，后续请求带上）
- **数据源**：`uploads/history.db`（SQLite），每次成功生成 PDF 都会写一条
- **容量上限**：200 万条，超出按 `id ASC` 淘汰最旧（FIFO）

---

## 1. 认证

所有 `/api/*` 接口要求已登录 session。

### 登录
```
POST /login
Content-Type: application/x-www-form-urlencoded

password=<LEGAL_NOTICE_PASSWORD>
```

- 成功：`302 Location: /`，`Set-Cookie: session=...`（Flask session）
- 失败：`401` + 带 `__ERROR__` 的 HTML

之后所有 API 调用在 `Cookie` 头带上这个 session。如果 session 过期，API 会返回：

```json
HTTP/1.1 401 Unauthorized
Content-Type: application/json

{"error": "auth required"}
```

### 登出
```
GET /logout
POST /logout
```

---

## 2. 接口列表

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/verify?serial=<SERIAL>` | 按序列号反查真伪 |
| `GET` | `/api/history?limit=&offset=&q=` | 分页浏览历史记录，可按姓名模糊搜 |

---

## 3. `GET /api/verify`

按律师函 **QR 下方印的 16 位防伪序列号**反查，返回当初发放时存下的 `name` / `principal` / `generated_at`。

### 请求参数

| 参数 | 位置 | 类型 | 必需 | 说明 |
|---|---|---|---|---|
| `serial` | Query | string | ✅ | `XXXX-XXXX-XXXX-XXXX`，最长 40 字符，只允许 `A–Z 0–9 -` |

客户端可传**带或不带 `-`**（`A7F3-9B2C-...` 或 `A7F39B2C...`），后端会做规范化（大写、去空白）。但最终在库里查的是带 `-` 的原始格式，所以**推荐客户端直接照 QR 下方印刷格式原样提交**。

### 成功响应

```
HTTP/1.1 200 OK
Content-Type: application/json
```
```json
{
  "ok": true,
  "name": "Ali Hassan",
  "principal": "50,000.00",
  "generated_at": "2026-04-16T12:35:59"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | bool | `true` 表示命中 |
| `name` | string | 当初发放时的被告姓名（Unicode，支持中英文 / 乌尔都文） |
| `principal` | string | Principal_Amount 字段，带千分位 + 2 位小数（`50,000.00`） |
| `generated_at` | string | ISO 8601，精确到秒，服务器本地时区 |

### 未命中响应

```
HTTP/1.1 404 Not Found
Content-Type: application/json
```
```json
{"ok": false, "error": "not found"}
```

含义：**这份律师函不是本系统生成的**（伪造，或者发自其他部署的 S&S 系统）。

### 参数错误

```
HTTP/1.1 400 Bad Request
Content-Type: application/json
```
```json
{"ok": false, "error": "invalid serial format"}
```

触发条件：`serial` 缺失、超长（>40）、含非法字符。

### 示例

```bash
curl -s -b cookies.txt \
  "https://example.com/api/verify?serial=67MX-4KPE-C9A9-YDE6" | jq
```

Python：

```python
import requests

session = requests.Session()
session.post("https://example.com/login", data={"password": "..."})
r = session.get("https://example.com/api/verify",
                params={"serial": "67MX-4KPE-C9A9-YDE6"})
if r.status_code == 200:
    data = r.json()
    print(f"✓ Authentic — issued to {data['name']} on {data['generated_at']}")
elif r.status_code == 404:
    print("✗ Not in our records — possibly forged")
```

### 同一姓名多次发放时的行为
一个被告有时会收到多次律师函。每次发放都会插入一行，**`/api/verify` 只返回该序列号对应的那一条**（`ORDER BY id DESC LIMIT 1`——但序列号几乎是 1 对 1，集合空间 32¹⁶ ≈ 1.2×10²⁴）。所以返回的是"发放这个具体序列号时"的现场。

---

## 4. `GET /api/history`

分页浏览全部历史记录，用于内部审计、对账、抽查。

### 请求参数

| 参数 | 位置 | 类型 | 默认 | 约束 | 说明 |
|---|---|---|---|---|---|
| `limit` | Query | int | 50 | 1–500 | 单页返回多少条 |
| `offset` | Query | int | 0 | ≥ 0 | 跳过多少条 |
| `q` | Query | string | `""` | 可选 | 按 `name` 做 SQL `LIKE '%q%'` 模糊匹配 |

分页采用经典 `limit/offset`。大 offset（万级以上）性能仍可接受，因为 `notice_history` 按 id 主键 DESC 排序，索引有序。

### 成功响应

```
HTTP/1.1 200 OK
Content-Type: application/json
```
```json
{
  "total": 12473,
  "limit": 50,
  "offset": 0,
  "rows": [
    {
      "serial": "67MX-4KPE-C9A9-YDE6",
      "name": "History Test",
      "principal": "50,000.00",
      "generated_at": "2026-04-16T12:35:59"
    },
    {
      "serial": "5B7F-U3YD-BV5X-JJXM",
      "name": "Ali Hassan",
      "principal": "50,000.00",
      "generated_at": "2026-04-16T12:10:12"
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `total` | int | 符合当前 `q` 的总条数（不受 `limit`/`offset` 影响） |
| `limit` | int | 实际返回的单页上限 |
| `offset` | int | 实际跳过的条数 |
| `rows` | array | 按 `id DESC` 排序（最新的在前） |
| `rows[].serial` | string | 16 位防伪码 |
| `rows[].name` | string | 被告姓名 |
| `rows[].principal` | string | Principal 金额字符串 |
| `rows[].generated_at` | string | ISO 8601 时间戳 |

### 参数错误

```
HTTP/1.1 400 Bad Request
```
```json
{"error": "limit/offset must be integers"}
```

### 示例

```bash
# 最近 20 条
curl -s -b cookies.txt \
  "https://example.com/api/history?limit=20" | jq

# 搜姓名含"Abbas"
curl -s -b cookies.txt \
  "https://example.com/api/history?q=Abbas&limit=100" | jq

# 翻第 2 页
curl -s -b cookies.txt \
  "https://example.com/api/history?limit=50&offset=50" | jq
```

Python（抓取全部记录到 CSV）：

```python
import csv, requests

session = requests.Session()
session.post("https://example.com/login", data={"password": "..."})

rows = []
offset = 0
while True:
    r = session.get("https://example.com/api/history",
                    params={"limit": 500, "offset": offset})
    d = r.json()
    rows.extend(d["rows"])
    if offset + d["limit"] >= d["total"]:
        break
    offset += d["limit"]

with open("history.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["serial", "name", "principal", "generated_at"])
    w.writeheader()
    w.writerows(rows)

print(f"exported {len(rows)} records")
```

---

## 5. 序列号（Serial）规范

`/api/verify` 的核心参数。

### 格式
```
XXXX-XXXX-XXXX-XXXX
```
- **总长度**：19 字符（16 位字母数字 + 3 个连字符）
- **字母表**：`ABCDEFGHJKLMNPQRSTUVWXYZ23456789`（32 字符，剔除 `0/O/1/I` 防止人眼误读）
- **大小写**：**大写**（API 接受小写输入，会自动 `.upper()`）

### 生成算法
```python
# legal_notice_gen.generate_qr_serial
import hashlib, random
def generate_qr_serial(name):
    seed = int.from_bytes(
        hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    chars = [rng.choice(alphabet) for _ in range(16)]
    return "-".join("".join(chars[i:i + 4]) for i in (0, 4, 8, 12))
```

**确定性**：同一个 `name` 在任何时间、任何机器、任何 Python 版本上运行，结果都相同。`/api/verify` 不依赖这个确定性（它只查库），但这是律所可以离线自检的手段——给定某一份律师函的姓名，能独立算出该份应有的序列号、和纸上印的对比。

### 冲突空间
32¹⁶ ≈ 1.2×10²⁴。生日悖论意义下，产生一次碰撞平均需要约 3.5×10¹² 个不同姓名。现实世界不会撞。

---

## 6. 错误码速查

| HTTP | 含义 | 示例场景 |
|---|---|---|
| `200` | 成功 | `/verify` 命中、`/history` 正常返回 |
| `400` | 参数错误 | serial 超长、limit 不是整数 |
| `401` | 未登录 | session 过期或没带 cookie |
| `404` | 未命中 | `/verify` 找不到该序列号 |
| `500` | 服务端错误 | 数据库损坏、磁盘满等，返回通用错误 |

所有 JSON 错误响应统一 shape：
```json
{"error": "<human-readable message>"}
```
（`/api/verify` 命中/未命中用的是 `{"ok": bool, ...}` 混合 shape，因为客户端会按 `ok` 字段分支。）

---

## 7. 限制 & 注意事项

### 写入来源
数据库只存**本系统生成的律师函**。如果律所以前用过其他工具或手动发过的律师函，**不会**在库里，`/api/verify` 会返回 404。这不是 bug，是预期——404 的正确解读是"不是本系统产出的"而不是"一定是伪造的"。

### 2 百万条上限
- `HISTORY_MAX_ROWS = 2_000_000`
- 到上限后**按 `id ASC` 删除最早的**，即 FIFO 淘汰
- 按每天 5000 条批量生成估算，2M 条 ≈ 400 天 ≈ 13 个月

如果需要保留更久，有两种升级路径：
1. 改 `HISTORY_MAX_ROWS`（SQLite 单表千万行量级仍健康，查询仍 <10 ms）
2. 定期 dump 出整表到归档数据库，然后 `DELETE` 老数据

### 时区
`generated_at` 用 **服务器本地时间**（`datetime.datetime.now()` 不带 tz）。部署前确认服务器 tz 或加时区字段。跨时区客户端解析时需要约定。

### 并发
SQLite WAL 模式允许多读 + 单写。`/api/verify` 和 `/api/history` 都是只读，与生成流水线的写入不冲突。多实例部署（多台机器共用同一 DB 文件 via 网络盘）**不被支持**——SQLite 在网络盘上锁语义不可靠。要跨实例共享需换 Postgres。

### 不记录的字段
DB 里只有 4 列：`serial`, `name`, `principal`, `generated_at`。**不存**：
- `cnic` / `phone`（隐私，留在原始 Excel 归律所自己管）
- `Transaction_id` / `easypaisa_account`（财务系统已有）
- `disb_date` / `Due_date`（业务数据）

这是故意保守——一旦 DB 文件泄露，泄露面最小。需要更多字段时改 schema + `_log_notice_record()`。

### 速率
当前接口**没有 rate limit**。认证用户可以高频调用 `/api/history`。部署到公网时，建议在 Nginx / Cloudflare 层加限流。

---

## 8. 未来扩展预留

以下是可能的接入点，列一下以便后续实现时有参考，**当前版本暂不实现**：

| 功能 | 可能接口 | 说明 |
|---|---|---|
| 按时间范围查 | `GET /api/history?from=2026-04-01&to=2026-04-30` | 审计月报 |
| 按 name 精确查 | `GET /api/inquiry/by_name?name=...` | 返回某人历次律师函 |
| 批量核验 | `POST /api/verify/batch` body `{serials:[...]}` | 一次核验多条 |
| CSV 导出 | `GET /api/history.csv?q=...` | 内网 OA 定期拉取 |
| 删除记录 | `DELETE /api/history/<serial>` | 合规删除（对账后） |
| HMAC 签名版 serial | `/api/verify?serial=...&sig=<hmac>` | 防撞库 |
| 只读 Token（非 session） | `X-API-Key: <token>` | 供第三方无状态接入 |
| Webhook 通知 | 新生成 → POST 到订阅 URL | 推模式核验 |

如果要对外（非律所内网）开放，**必须**先做：
1. HTTPS（Let's Encrypt / Cloudflare）
2. rate limit（Nginx `limit_req` 或 Flask-Limiter）
3. 独立的 API token（不要复用工作台的密码）
4. `/api/verify` 加 CAPTCHA 或 CAPTCHA-equivalent 抗暴力枚举

---

## 9. 数据模型参考

`uploads/history.db` 结构（SQLite + WAL）：

```sql
CREATE TABLE notice_history (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  serial       TEXT NOT NULL,          -- 'XXXX-XXXX-XXXX-XXXX'
  name         TEXT NOT NULL,
  principal    TEXT,                   -- '50,000.00' 字符串
  generated_at TEXT NOT NULL           -- '2026-04-16T12:35:59'
);
CREATE INDEX idx_history_serial  ON notice_history(serial);
CREATE INDEX idx_history_created ON notice_history(generated_at);

PRAGMA journal_mode = WAL;
```

直接查库（调试用）：
```bash
sqlite3 uploads/history.db \
  "SELECT serial, name, principal, generated_at
   FROM notice_history
   WHERE name LIKE '%Abbas%'
   ORDER BY id DESC LIMIT 20;"
```

---

## 10. 版本历史

- **v1（2026-04-16）**：初版上线，`/api/verify` + `/api/history` 两个只读接口，SQLite 存储，2M 条上限
