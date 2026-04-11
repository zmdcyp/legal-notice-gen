# 律师函批量生成工具

从 docx 模板 + Excel 数据，批量生成律师函，支持 PDF 和 DOCX 输出，支持按任意 Excel 列分组打包（例如 `agent`）。面向大批量（5000+ 条）设计。

## 设计原则

### 占位符驱动，业务内容无关

**本工具只做占位符替换，不理解、不校验模板或表格的业务内容。**

- 模板里 `{{字段名}}` 会被 Excel 中同名列的值替换
- 模板写什么、字段叫什么、Excel 表头如何命名，工具都不关心
- 只要模板占位符与 Excel 表头对得上，就能出活
- 所以它不只能生成律师函 —— 任意"docx 模板 + Excel 数据批量出文档"的场景都能用

### 单文件交付

整个程序是一个 Python 文件 `legal_notice_gen.py`，HTML、CSS、JS 全部内联，没有外部模板、没有静态资源目录。交付给其他人时只需要发送：

```
legal_notice_gen.py
requirements.txt
```

加上目标机器上安装的 Python 和 LibreOffice，就可以直接 `python legal_notice_gen.py` 启动。

## 功能特性

- **模板驱动** — 使用 Word (.docx) 文件作为模板，保留 letterhead、制式内容和排版格式
- **占位符替换** — 模板中用 `{{字段名}}` 标记可变内容；支持正文段落、表格、页眉、页脚
- **智能匹配** — 自动分析模板占位符与 Excel 列名的匹配情况，未匹配字段页面手填（所有行共享）
- **多字段文件命名** — 可在网页中勾选任意多个 Excel 列作为文件名，用 `_` 连接（工具不预设具体字段，都由你在界面选）
- **按字段分组打包** — 指定分组列（如 `agent`），输出一个外层 zip，内含一个个以分组值命名的子 zip
- **PDF / DOCX 输出** — 默认 PDF（通过 LibreOffice headless），也可直接输出 DOCX
- **日期处理** — Excel 日期单元格自动格式化为 `YYYY-MM-DD`
- **整数型数字处理** — Excel 整数列不会出现 `1234.0` 的尾巴
- **异步任务 + 进度条** — 长时间任务不会卡请求，界面实时显示 rendering / converting / packing 阶段进度
- **高并发生成** — docx 渲染和 PDF 转换都是多线程并行；PDF 走批量 soffice 调用，5000 量级可用

## 环境要求

- Python 3.8+
- LibreOffice（仅 PDF 输出需要）
- `pip install -r requirements.txt`（flask / python-docx / openpyxl）

## 本地运行

```bash
pip install -r requirements.txt

# macOS 安装 LibreOffice
brew install --cask libreoffice

python legal_notice_gen.py
# 浏览器访问 http://127.0.0.1:5002
```

本地模式使用 Flask 内置服务器 + 多线程，适合 5000 以内的批量处理。

## VPS 部署（Ubuntu/Debian）

将项目上传到 VPS 后执行一键部署脚本：

```bash
scp -r legal_notice_gen/ root@你的VPS地址:/root/
ssh root@你的VPS地址
cd /root/legal_notice_gen
sudo bash deploy/setup.sh
```

脚本自动完成：

1. 安装 Python3、LibreOffice headless、中文字体（文泉驿）、Nginx
2. 创建专用用户 `legalnotice`，部署到 `/opt/legal_notice_gen`
3. 创建 Python 虚拟环境，安装依赖 + Gunicorn
4. 配置 systemd 服务：**`--workers 1 --threads 16 --worker-class gthread --timeout 1800`**
5. 配置 Nginx 反向代理：`client_max_body_size 100M`, `proxy_read_timeout 1800s`, `proxy_buffering off`

> ⚠️ **为什么 Gunicorn 必须 1 worker：** 后台任务状态（进度、结果路径）保存在进程内存的 `TASKS` 字典里。`/status` 轮询和 `/download` 必须落到同一个进程才能查到任务。多 worker 会导致请求命中其他进程，拿不到状态。用 `--threads 16` 获得并发，而不是 `--workers N`。

### 常用运维命令

```bash
systemctl status legal_notice_gen
journalctl -u legal_notice_gen -f
systemctl restart legal_notice_gen
```

### 配置 HTTPS（可选）

```bash
apt install certbot python3-certbot-nginx
certbot --nginx -d 你的域名
```

## 使用说明

### 1. 准备 docx 模板

在 Word 文档中用 `{{字段名}}` 标记所有可变内容。字段名由你自己决定 —— 工具不预设任何字段，只按占位符文本做替换。

- 占位符语法：`{{字段名}}`
- 占位符可出现在正文、表格、页眉、页脚
- 字段名支持中文、英文、数字、空格

### 2. 准备 Excel 数据

- 第一行为表头，**表头名称必须与模板占位符完全一致**（大小写和空格敏感）
- 每行数据生成一个独立文件
- 模板中有但 Excel 中没有的占位符，可在页面手动填写（应用到所有行）
- 全空行自动跳过
- 整数型数字列（如身份证号、电话）不会出现 `.0` 尾巴

### 3. 上传并在网页中选择字段

1. 上传 docx 模板 + Excel 数据，点击 **Analyze Files**
2. 查看占位符匹配情况：
   - 绿色 tag：Excel 有对应列
   - 红色 tag：Excel 里没有，需要在页面下方手填（统一应用到所有行）
3. **File Naming** —— 在 checkbox 列表里勾选一列或多列作为文件名的组成部分，用 `_` 连接
   - 默认没有任何列被预勾选；完全由你在网页里选择
   - 未勾选任何列则退化为顺序编号 `notice_0001.pdf`
4. **Group By** —— 可选下拉框，指定用作分组的列（例如 `agent`）
   - 设置后：输出一个外层 `legal_notices.zip`，内含若干以分组值命名的子 zip
   - 不设置：输出单个平铺 zip
5. **Output Format** —— PDF（默认）或 DOCX
6. 点 **Generate Notices**，等待进度条跑完，自动下载

## 5000 量级批量性能说明

针对 5000 条左右批量做了以下优化：

1. **并行 docx 渲染**：`ThreadPoolExecutor(MAX_WORKERS)` 并发 `Document(template) → 替换 → save`。
2. **并行 PDF 转换**：所有 docx 被均分成 N 批，每批一次 `soffice --convert-to pdf file1 file2 ... fileN` 调用，N 个 soffice 进程同时跑。
3. **soffice profile 隔离**：每个 worker 用独立的 `-env:UserInstallation=file:///tmp/soffice_profile_N` 目录，避免 LibreOffice profile 锁冲突（这是让 soffice 真正并行的关键）。
4. **异步任务模型**：`/generate` 启动后台线程立即返回 `task_id`；前端每秒轮询 `/status/<task_id>` 拿到阶段和进度；完成后才 GET `/download/<task_id>`。这样避免了 HTTP 请求级的 Nginx / Gunicorn 超时限制 —— 一个 5000 条的 PDF 任务可以跑 20 分钟，也不会断连。
5. **批量 soffice 调用**：每个 worker 一次 soffice 调用处理多个文件，而不是一个文件一次调用 —— soffice 单次启动有 1~2 秒开销，5000 次就是小时级；批量合并后开销可忽略。

### 实测参考值

- 4 核 VPS、`MAX_WORKERS=4`：5000 条 PDF 大约 10~20 分钟（主要瓶颈是 LibreOffice 本身）
- 想更快：加 CPU 核心数；或改输出 DOCX 跳过 PDF 转换（秒级完成 5000 条）

### 容量上限参考

- 内存：最终 zip 会落盘到 `uploads/result_<taskid>.zip`，不是 BytesIO，所以即使是 GB 级压缩包也不压 Python 内存
- 结果文件 5 分钟后自动清理；老 task 1 小时后从 `TASKS` 字典里剔除

## 项目结构

```
legal_notice_gen/
├── legal_notice_gen.py   # 主程序 —— 后端 + HTML + CSS + JS 全部内联
├── requirements.txt      # Python 依赖
├── deploy/
│   └── setup.sh          # VPS 一键部署脚本
├── readme.md             # 本文档
└── uploads/              # 运行时目录（session 文件、结果 zip、任务临时目录）
```

## 技术栈

- **后端**：Python / Flask（单文件、内联模板）
- **并发**：`concurrent.futures.ThreadPoolExecutor` + `threading`
- **模板处理**：python-docx / openpyxl
- **PDF 转换**：LibreOffice headless，多进程并行、批量调用
- **生产部署**：Gunicorn (1 worker, 16 threads, gthread) + Nginx + systemd

## 异步任务 API

- `POST /upload` — 上传 template + excel，分析占位符匹配情况
- `POST /generate` — 启动后台生成任务，立即返回 `{task_id}`
- `GET /status/<task_id>` — 返回 `{status, stage, progress, total, message, error}`
  - `status`: `pending | running | done | error`
  - `stage`: `queued | rendering | converting | packing | done`
- `GET /download/<task_id>` — 下载最终 zip（仅当 `status == "done"` 时可用）

前端会每秒轮询 `/status` 直到 `done`，然后自动触发下载。

## 实现细节

- **占位符正则**：`\{\{(.+?)\}\}`（`legal_notice_gen.py`）
- **Run 合并**：Word 经常把一个 `{{字段}}` 拆到多个 run 中，`_replace_in_paragraph` 先把段落所有 run 文本拼起来做替换再写回第一个 run、清空其余 run。**副作用**：段落内原有的多 run 字符级格式（局部加粗等）会被合并为第一个 run 的格式。
- **手动字段优先级**：生成时 `{**manual_fields, **record}`，Excel 行数据覆盖页面手填的同名字段。
- **文件名去重**：同一分组内相同文件名会自动加数字后缀 `_2`、`_3`。
- **安全文件名**：过滤 `\ / * ? : " < > | \r \n \t`，空字符串降级为 `unnamed`。
- **soffice 超时**：`max(180, len(batch) * 15)` 秒。
- **Secret key**：持久化到 `uploads/.secret_key`，避免进程重启丢 session。
- **任务 TTL**：完成/失败任务 1 小时后被 `_prune_tasks` 清理；结果 zip 在被下载 5 分钟后删除。

## 已知事项

- 后台任务状态驻留在进程内存，重启服务会丢失进行中的任务。如需跨进程/跨重启持久化，需要引入 Redis/RQ/Celery —— 本项目为了保持单文件交付没有引入。
