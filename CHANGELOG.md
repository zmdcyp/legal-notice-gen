# 更新日志

本文件记录项目的重要变更。格式：按日期倒序，最新的变更在最上面。

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
