#!/bin/bash
# 律师函批量生成工具 - Ubuntu/Debian VPS 一键部署脚本
# 用法: sudo bash setup.sh

set -e

APP_NAME="legal_notice_gen"
APP_DIR="/opt/$APP_NAME"
APP_USER="legalnotice"
DOMAIN=""  # 如有域名，填入后会配置 HTTPS

# 访问密码：留空就用源码里 _DEFAULT_PASSWORD（不推荐生产用）
# 非交互式部署时先 `export LEGAL_NOTICE_PASSWORD=...` 再跑本脚本。
APP_PASS="${LEGAL_NOTICE_PASSWORD:-}"
if [ -z "$APP_PASS" ] && [ -t 0 ]; then
    echo -n "Set LEGAL_NOTICE_PASSWORD (回车跳过,用源码默认密码): "
    read -rs APP_PASS
    echo
fi

echo "================================================"
echo "  律师函批量生成工具 - VPS 部署"
echo "================================================"

# 1. 安装系统依赖
echo "[1/6] 安装系统依赖..."
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv nginx \
    fonts-wqy-zenhei fonts-wqy-microhei \
    fonts-dejavu-core fonts-noto-core fonts-noto-nastaliq-urdu \
    libjpeg-dev zlib1g-dev > /dev/null

# Playwright needs a pile of shared libs for headless Chromium. `playwright
# install-deps` pulls the right apt packages for the current distro.

# 2. 创建应用用户
echo "[2/6] 创建应用用户..."
if ! id "$APP_USER" &>/dev/null; then
    useradd -r -s /bin/false -m -d "$APP_DIR" "$APP_USER"
fi

# 3. 部署应用代码
echo "[3/6] 部署应用代码..."
mkdir -p "$APP_DIR"
cp legal_notice_gen.py "$APP_DIR/"
cp requirements.txt "$APP_DIR/"
# templates/ 包含 base HTML + 字体 + 出厂 PNG —— 运行时必须存在。
# 递归复制以保留 static/fonts/*.ttf 和 static/images/*.png。
cp -r templates "$APP_DIR/"

cd "$APP_DIR"
python3 -m venv venv
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt gunicorn
# Download Chromium used for HTML→PDF rendering (~170 MB) into the venv.
playwright install-deps chromium
playwright install chromium
deactivate

mkdir -p "$APP_DIR/uploads"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# 4. 创建 systemd 服务
# 重要：task 状态保存在进程内存中，必须使用 --workers 1 + --threads N，
# 否则 /status 和 /download 轮询会落到其他 worker 拿不到任务状态。
# --timeout 1800 让单个后台任务最多跑 30 分钟。
echo "[4/6] 配置 systemd 服务..."
PASS_ENV=""
if [ -n "$APP_PASS" ]; then
    # systemd Environment 行需要转义双引号
    ESC_PASS=$(printf '%s' "$APP_PASS" | sed 's/"/\\"/g')
    PASS_ENV="Environment=\"LEGAL_NOTICE_PASSWORD=${ESC_PASS}\""
fi

cat > /etc/systemd/system/$APP_NAME.service << EOF
[Unit]
Description=律师函批量生成工具
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment="PATH=$APP_DIR/venv/bin:/usr/bin"
$PASS_ENV
ExecStart=$APP_DIR/venv/bin/gunicorn \\
    --bind 127.0.0.1:5002 \\
    --workers 1 \\
    --threads 16 \\
    --worker-class gthread \\
    --timeout 1800 \\
    --graceful-timeout 60 \\
    legal_notice_gen:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable $APP_NAME
systemctl start $APP_NAME

# 5. 配置 Nginx 反向代理
# client_max_body_size 100M: 允许大 Excel / 模板。
# proxy_read_timeout 1800s: 允许 /download 在大批量生成时长时间流式传输。
echo "[5/6] 配置 Nginx..."
cat > /etc/nginx/sites-available/$APP_NAME << 'EOF'
server {
    listen 80;
    server_name _;
    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:5002;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 1800s;
        proxy_send_timeout 1800s;
        proxy_buffering off;
    }
}
EOF

ln -sf /etc/nginx/sites-available/$APP_NAME /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# 6. 完成
echo "[6/6] 部署完成!"
echo ""
echo "================================================"
echo "  服务已启动!"
echo "  访问: http://$(hostname -I | awk '{print $1}')"
if [ -z "$APP_PASS" ]; then
    echo ""
    echo "  ⚠ 使用源码里的默认密码（仓库公开，风险!）"
    echo "    改密码: edit /etc/systemd/system/$APP_NAME.service"
    echo "            加一行 Environment=\"LEGAL_NOTICE_PASSWORD=<your-pass>\""
    echo "            然后 systemctl daemon-reload && systemctl restart $APP_NAME"
fi
echo ""
echo "  常用命令:"
echo "    查看状态:  systemctl status $APP_NAME"
echo "    查看日志:  journalctl -u $APP_NAME -f"
echo "    重启服务:  systemctl restart $APP_NAME"
echo "    更新代码:  cd <本地项目> && scp -r . root@VPS:$APP_DIR/ && systemctl restart $APP_NAME"
echo "================================================"
