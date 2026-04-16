# VPS 部署指南

把工作台跑到自己的 VPS 上，用 Nginx + Gunicorn 对外。预计 **10 分钟**。

仓库里有一键脚本 `deploy/setup.sh`，大多数情况直接 `sudo bash` 它就行。这篇文档解释每一步在做什么、出问题怎么查。

---

## 前置要求

| 项 | 要求 |
|---|---|
| 系统 | Ubuntu 22.04 / 24.04 或 Debian 12+（脚本是给这两家写的） |
| 内存 | **≥ 2 GB**（Chromium ≈ 300 MB × 1 worker） |
| 磁盘 | ≥ 2 GB 空闲（Chromium ~170 MB + 字体 ~100 MB + 历史 DB / PDF 缓存） |
| 网络 | 80/443 端口开放 |
| 权限 | root 或有 sudo 的用户 |
| 其他 | 能连到 VPS 的 SSH |

内存小于 2 GB 也能跑，但大批量时容易 OOM。4 GB 以上可以切 Mac profile 提高并发。

---

## 部署流程

### 1. 代码上 VPS

**方法 A：从 GitHub clone（推荐）**
```bash
ssh root@YOUR_VPS_IP
apt-get update && apt-get install -y git
git clone https://github.com/zmdcyp/legal-notice-gen.git /root/legal_notice_gen
cd /root/legal_notice_gen
```

**方法 B：scp 本地代码（repo 是 private 或想推未提交的改动）**
```bash
# 本地机器：
cd /path/to/legal_notice_gen
scp -r . root@YOUR_VPS_IP:/root/legal_notice_gen/
# SSH 上去：
ssh root@YOUR_VPS_IP
cd /root/legal_notice_gen
```

### 2. 跑部署脚本

**交互式**（推荐）：脚本会提示输入访问密码
```bash
sudo bash deploy/setup.sh
```

**非交互式**（CI / 自动化）：先 `export` 再跑
```bash
export LEGAL_NOTICE_PASSWORD='一个强密码'
sudo -E bash deploy/setup.sh    # -E 保留 env var
```

### 3. 脚本会做的 6 步

| 步骤 | 干啥 | 大概多久 |
|---|---|---|
| 1/6 · 系统依赖 | `apt-get install` Python、Nginx、Noto Urdu 字体、中文字体（WenQuanYi）、DejaVu 核心、图像开发库 | 30 s |
| 2/6 · 应用用户 | 建 `legalnotice` 系统用户（不带 shell，home = `/opt/legal_notice_gen`） | 1 s |
| 3/6 · 代码 + venv | 拷 `.py` + `requirements.txt` + **`templates/`** 到 `/opt/...`；建 venv；`pip install -r requirements.txt + gunicorn`；`playwright install chromium` 下 Chromium 二进制 | 2–3 min |
| 4/6 · systemd | 写 unit 文件（**`--workers 1 --threads 16`**、带密码 env var）；enable + start | 1 s |
| 5/6 · Nginx | 写反代配置（`client_max_body_size 100M`、`proxy_read_timeout 1800s`、禁缓冲）；`nginx -t && reload` | 1 s |
| 6/6 · 完成 | 打印公网 IP + 常用命令 | instant |

### 4. 验证

```bash
# 本地健康检查（不经过 Nginx）
curl -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5002/
# 应返回 302（未登录重定向到 /login）

# 经过 Nginx
curl -o /dev/null -w "%{http_code}\n" http://127.0.0.1/
# 同样 302

systemctl status legal_notice_gen    # Active: running (green)
journalctl -u legal_notice_gen -f    # 实时日志
```

浏览器打开 `http://YOUR_VPS_IP/`，用部署时设的密码登录。看到工作台就成功了。

---

## 关键架构细节

### 为什么 Gunicorn 必须 `--workers 1`？

任务状态（`TASKS` dict）、Chromium 共享池（`_SHARED_SINGLE_BROWSER`）、LRU 缓存（overlay / QR）、历史 DB 句柄都**在进程内存里**。多 worker 会让：

- `/generate` 在 worker-1 启动了一个 task_id
- 浏览器轮询 `/status/<task_id>` 被 Nginx 转到 worker-2
- worker-2 的 `TASKS` 里没这个 id → 返回 404
- 用户看到"任务消失"

并发靠 `--threads 16` 在同一进程里的多线程做，足够应付 5000 条量级 + 几个用户同时在线。要跨 worker 持久化需要 Redis/Celery（本项目没引入）。

### 为什么内存紧？

每个 Chromium 渲染中约 300 MB RSS。VPS profile 只允许 1 个，Mac profile 允许 6 个。**2 GB VPS 只跑 VPS profile**，别切 Mac，不然 Chromium OOM systemd 会一直重启。

单条渲染的中间产物：
- 300 dpi A4 像素图 ≈ 35 MB/page
- 两页 + rasterize 中间副本 ≈ 150–200 MB 临时峰值
- 正常 return 后立刻 GC 掉

### Playwright 装在 venv 里

脚本跑的是 `venv/bin/playwright install chromium`——Chromium 二进制下到 venv 的 cache（`~/.cache/ms-playwright/` 或类似），不是系统级。卸载直接 `rm -rf /opt/legal_notice_gen` 就带走。

---

## 配置调优

### 访问密码

**部署时没填** → 用源码里 `legal_notice_gen.py:94` 的 `RT%L6IXoXT*^r=z6%npe`。**仓库公开**，公网部署一定要改。

**改密码**：
```bash
sudo vim /etc/systemd/system/legal_notice_gen.service
# 在 [Service] 块里加一行：
#   Environment="LEGAL_NOTICE_PASSWORD=你的新密码"
# 已经有就直接改值
sudo systemctl daemon-reload
sudo systemctl restart legal_notice_gen
```

登录用的是 `hmac.compare_digest` 常量时间比较，没有时序泄漏。但也**没有 rate limit**——公网上要防暴力猜密码，在 Nginx 加一条：
```nginx
location = /login {
    limit_req zone=login burst=5 nodelay;
    proxy_pass http://127.0.0.1:5002;
    # ... 其他 proxy 头
}
# http {} 块里定义 zone：
#   limit_req_zone $binary_remote_addr zone=login:10m rate=10r/m;
```

### HTTPS（公网必配）

Let's Encrypt 一键：
```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
# 自动改 Nginx 配 + 装续期 cron
```

没有域名？用 Cloudflare Tunnel（免费、免开端口）：
```bash
# 装 cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cf.deb
sudo dpkg -i cf.deb
cloudflared tunnel login
cloudflared tunnel create legal-notice
cloudflared tunnel route dns legal-notice legal-notice.your-domain.com
# 写 config.yml 指 127.0.0.1:5002
cloudflared service install
```

### Machine Profile

VPS profile 默认 1 个 Chromium worker；Mac profile 最多 6 个。选择在**生成页**而非部署时。机器真有余裕（4 GB+ RAM、4+ cores）就在网页生成时选 Mac。

想长期默认跑更高并发，改源码 `MACHINE_PROFILES`（legal_notice_gen.py 的靠前部分），或者加新档位：

```python
MACHINE_PROFILES = {
    "vps": {"render_workers": 1, "label": "VPS (1 Chromium worker)"},
    "vps-4g": {"render_workers": 2, "label": "VPS 4 GB (2 workers)"},
    "mac": {"render_workers": min(_CPU, 6), "label": f"Mac (up to {min(_CPU, 6)} workers)"},
}
```

### 历史数据库备份

`uploads/history.db` 是单文件 SQLite。定期备份：
```bash
# 放 cron
sqlite3 /opt/legal_notice_gen/uploads/history.db \
  ".backup /backup/history-$(date +%F).db"

# 或每天 rsync 走
rsync -az /opt/legal_notice_gen/uploads/history.db backup-host:/backups/
```

恢复：直接 `cp` 覆盖回去，重启服务即可。

**容量上限 200 万条**。按每天 5000 条算约 400 天。到顶后 FIFO 删最旧。要永久保留就调大 `HISTORY_MAX_ROWS`（SQLite 千万行量级仍健康）或改成按年归档。

---

## 更新代码

**从 GitHub 拉（如果是 git clone 的）：**
```bash
ssh root@YOUR_VPS_IP
cd /opt/legal_notice_gen
sudo -u legalnotice git pull
sudo systemctl restart legal_notice_gen
```

**从本地 scp 覆盖（如果原始是 scp 部署的）：**
```bash
# 本地：
cd /path/to/legal_notice_gen
rsync -av --exclude=uploads --exclude=venv --exclude=__pycache__ \
    . root@YOUR_VPS_IP:/opt/legal_notice_gen/
# 远端：
ssh root@YOUR_VPS_IP 'systemctl restart legal_notice_gen'
```

**升级 Python 依赖：**
```bash
cd /opt/legal_notice_gen
sudo -u legalnotice venv/bin/pip install -r requirements.txt --upgrade
sudo systemctl restart legal_notice_gen
```

**升级 Chromium：**
```bash
cd /opt/legal_notice_gen
sudo -u legalnotice venv/bin/playwright install chromium
sudo systemctl restart legal_notice_gen
```

---

## 常见问题

| 症状 | 原因 | 修复 |
|---|---|---|
| 502 Bad Gateway | gunicorn 没起 | `journalctl -u legal_notice_gen -n 80` 看错误；常见是 playwright 没装完或密码 env var 格式错 |
| 启动后打不开 `/` | Nginx 没 reload / 防火墙 | `nginx -t && systemctl reload nginx`；`ufw allow 80,443/tcp` |
| "Template not found" | `templates/` 没拷 | 旧 `setup.sh` 有 bug；本仓库 commit `5e2cb96` 已修，重跑 `sudo bash deploy/setup.sh` 或 `cp -r /root/legal_notice_gen/templates /opt/legal_notice_gen/ && chown -R legalnotice:legalnotice /opt/legal_notice_gen/templates` |
| 生成中途服务重启 | OOM | `dmesg \| grep -i kill`；加 swap 或降低 Machine Profile |
| Chromium 提示 `Host system is missing dependencies to run browsers` | playwright apt 依赖没装全 | `cd /opt/legal_notice_gen && sudo venv/bin/playwright install-deps chromium` |
| 乌尔都文 / 中文显示方框 | 字体没装 | `apt-get install -y fonts-noto-nastaliq-urdu fonts-wqy-zenhei fonts-wqy-microhei` |
| 生成 PDF 里水印没有 | `arabic_reshaper` / `python-bidi` 缺 | `pip install arabic_reshaper python-bidi`；看 `/static/fonts.css` 能否加载 |
| 访问慢、iframe 预览延迟 | CPU 忙 | 预览是前端 300ms 防抖 + 后端 LRU 缓存，CPU 100% 时还是慢；升 vCPU 或减 workers |
| `/verify` 查不到东西 | 历史 DB 空 / 文件权限 | `ls -la /opt/legal_notice_gen/uploads/history.db`；owner 应为 `legalnotice`；手动生成一条看有没有写 |
| 下载 zip 超时断开 | Nginx `proxy_read_timeout` 不够 | 已配 1800s；5000+ 条超大批量再调高 |
| SQLite `database is locked` | 多 worker 同写 | 应该只有 1 worker；如果你改过 `--workers`，改回 1 |

---

## 监控和维护

### 看日志
```bash
journalctl -u legal_notice_gen -f          # 实时
journalctl -u legal_notice_gen --since "1 hour ago"
journalctl -u legal_notice_gen -p err      # 只看错误
```

### 定期检查
- **磁盘**：`uploads/` 里的 `part_*.zip`（5 分钟 TTL 应自动清）、`notice_one_*` 临时目录（生成完应自动清）。若不清理要查代码的 `shutil.rmtree` 是不是被 `ignore_errors=True` 吞错了
- **内存**：`free -h` / `systemctl status legal_notice_gen` 看 Memory 行
- **历史 DB 体积**：`ls -lh uploads/history.db`；到上限前会 FIFO 不会爆炸

### 接外部监控（可选）
- **Uptime Kuma**（自建）：监控 HTTP 200 + 登录页能返回
- **Sentry**：捕获 Python exception；在 `legal_notice_gen.py` 顶部加几行 sentry_sdk.init
- **Prometheus + Grafana**：gunicorn 的 stats 可以用 statsd exporter 吐出来

---

## 卸载

```bash
sudo systemctl stop legal_notice_gen
sudo systemctl disable legal_notice_gen
sudo rm /etc/systemd/system/legal_notice_gen.service
sudo rm /etc/nginx/sites-enabled/legal_notice_gen
sudo rm /etc/nginx/sites-available/legal_notice_gen
sudo nginx -t && sudo systemctl reload nginx
sudo rm -rf /opt/legal_notice_gen
sudo userdel -r legalnotice    # -r 同时删 home（即 /opt/legal_notice_gen，保险起见再 rm 一次）
```

字体 / 系统依赖（python3、nginx 等）通常留着给其他服务用。

---

## 可选增强（按需实施）

| 增强 | 难度 | 收益 |
|---|---|---|
| HTTPS via Let's Encrypt | 5 min | 必备（公网） |
| Cloudflare 放前面 | 10 min | 免费 CDN + DDoS 防护 + 隐藏源 IP |
| `/login` rate limit（Nginx `limit_req`） | 10 min | 防暴力猜密码 |
| Fail2ban 封 IP | 15 min | 配合 Nginx rate limit 更狠 |
| 独立的只读 API token（对 `/api/verify`） | 20 min | 给内部 OA / 合作方调用时别用主密码 |
| SSH key-only + 禁 root 登录 | 10 min | 基本 VPS 安全 |
| Docker 化 | 1 h | 迁移 / 回滚 / 多实例便利 |
| 多机部署（负载均衡 + 共享 Postgres） | 半天 | 日万级以上，要改代码（替换进程内 TASKS dict + SQLite） |

---

## 参考

- 工作台使用：[`readme.md`](readme.md)
- 防伪机制细节：[`ANTI_COUNTERFEIT.md`](ANTI_COUNTERFEIT.md)
- 查询接口（供外部系统接入）：[`INQUIRY_API.md`](INQUIRY_API.md)
- 更新日志：[`CHANGELOG.md`](CHANGELOG.md)
