#!/bin/bash
# 律师函批量生成工具 - Ubuntu/Debian VPS 一键部署脚本
# 用法: sudo bash setup.sh

set -e

APP_NAME="legal_notice_gen"
APP_DIR="/opt/$APP_NAME"
APP_USER="legalnotice"
DOMAIN=""  # 如有域名，填入后会配置 HTTPS

echo "================================================"
echo "  律师函批量生成工具 - VPS 部署"
echo "================================================"

# 1. 安装系统依赖
echo "[1/6] 安装系统依赖..."
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv nginx libreoffice-writer fonts-wqy-zenhei fonts-wqy-microhei > /dev/null

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

cd "$APP_DIR"
python3 -m venv venv
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt gunicorn
deactivate

mkdir -p "$APP_DIR/uploads"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# 4. 创建 systemd 服务
echo "[4/6] 配置 systemd 服务..."
cat > /etc/systemd/system/$APP_NAME.service << 'EOF'
[Unit]
Description=律师函批量生成工具
After=network.target

[Service]
Type=simple
User=legalnotice
Group=legalnotice
WorkingDirectory=/opt/legal_notice_gen
Environment="PATH=/opt/legal_notice_gen/venv/bin:/usr/bin"
ExecStart=/opt/legal_notice_gen/venv/bin/gunicorn \
    --bind 127.0.0.1:5002 \
    --workers 2 \
    --timeout 120 \
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
echo "[5/6] 配置 Nginx..."
cat > /etc/nginx/sites-available/$APP_NAME << 'EOF'
server {
    listen 80;
    server_name _;
    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:5002;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
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
echo ""
echo "  常用命令:"
echo "    查看状态:  systemctl status $APP_NAME"
echo "    查看日志:  journalctl -u $APP_NAME -f"
echo "    重启服务:  systemctl restart $APP_NAME"
echo "================================================"
