# 律师函批量生成工具

从 docx 模板 + Excel 数据，批量生成律师函，支持 PDF 和 DOCX 输出。

## 功能特性

- **模板驱动** — 使用 Word (.docx) 文件作为模板，保留 letterhead、制式内容和排版格式
- **占位符替换** — 模板中用 `{{字段名}}` 标记可变内容，Excel 表头与占位符名称对应
- **签名/印章** — 支持上传透明底 PNG 图片，自动插入到 `{{sign}}` 和 `{{stamp}}` 占位符位置
- **智能匹配** — 自动分析模板占位符与 Excel 列名的匹配情况，未匹配的字段可在页面手动填写
- **批量生成** — Excel 每行数据生成一个独立文件，支持自定义文件命名字段
- **PDF 输出** — 通过 LibreOffice 转换，排版与原始 docx 完全一致
- **日期处理** — Excel 中的日期自动格式化为 `YYYY-MM-DD` 短日期格式
- **Web 界面** — 浏览器操作，三步完成：上传 → 匹配 → 生成下载

## 环境要求

- Python 3.8+
- LibreOffice（用于 docx → PDF 转换）

## 本地运行

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# macOS 安装 LibreOffice
brew install --cask libreoffice

# 启动服务
python legal_notice_gen.py

# 浏览器访问
open http://127.0.0.1:5002
```

## VPS 部署（Ubuntu/Debian）

将项目上传到 VPS 后执行一键部署脚本：

```bash
scp -r legal_notice_gen/ root@你的VPS地址:/root/
ssh root@你的VPS地址
cd /root/legal_notice_gen
sudo bash deploy/setup.sh
```

脚本自动完成以下操作：

1. 安装 Python3、LibreOffice (headless)、中文字体（文泉驿）、Nginx
2. 创建 Python 虚拟环境，安装依赖 + Gunicorn
3. 配置 systemd 服务（开机自启、崩溃自动重启）
4. 配置 Nginx 反向代理（80 端口）

部署后直接访问 `http://VPS的IP地址` 即可使用。

### 常用运维命令

```bash
# 查看服务状态
systemctl status legal_notice_gen

# 查看实时日志
journalctl -u legal_notice_gen -f

# 重启服务
systemctl restart legal_notice_gen
```

### 配置 HTTPS（可选）

```bash
apt install certbot python3-certbot-nginx
certbot --nginx -d 你的域名
```

## 使用说明

### 1. 准备 docx 模板

在 Word 文档中用双花括号标记可变内容：

```
致：{{收件人}}
地址：{{收件地址}}

本律师受{{委托人}}的委托，就{{案由}}事宜……

律师签名：{{sign}}
印章：{{stamp}}
日期：{{发函日期}}
```

- 文本占位符：`{{字段名}}` — 用 Excel 中对应列的值替换
- 签名占位符：`{{sign}}` — 用上传的签名 PNG 图片替换
- 印章占位符：`{{stamp}}` — 用上传的印章 PNG 图片替换

### 2. 准备 Excel 数据

| 收件人 | 收件地址 | 委托人 | 案由 | 发函日期 |
|--------|----------|--------|------|----------|
| 张三   | 北京市XX路 | 李四 | 合同纠纷 | 2026-04-01 |
| 某公司 | 上海市XX路 | 赵六 | 侵权纠纷 | 2026-04-01 |

- 第一行为表头，表头名称必须与模板中的占位符名称一致
- 每行数据生成一个独立的律师函文件
- 模板中有但 Excel 中没有的占位符，可在页面上手动填写（适用于所有行）

### 3. 上传并生成

1. 打开 Web 界面，上传 docx 模板和 Excel 文件
2. （可选）上传签名和印章的透明底 PNG 图片，调整图片宽度
3. 查看占位符匹配情况，填写未匹配的字段
4. 选择输出格式（PDF / DOCX）和文件命名方式
5. 点击「生成律师函」，下载 ZIP 压缩包

## 项目结构

```
legal_notice_gen/
├── legal_notice_gen.py   # 主程序（Flask Web 应用）
├── requirements.txt      # Python 依赖
├── create_sample.py      # 生成示例模板和数据的脚本
├── deploy/
│   └── setup.sh          # VPS 一键部署脚本
├── sample_template.docx  # 示例模板
└── sample_data.xlsx      # 示例数据
```

## 技术栈

- **后端**：Python / Flask
- **模板处理**：python-docx（读写 docx）、openpyxl（读取 Excel）
- **PDF 转换**：LibreOffice headless 模式
- **生产部署**：Gunicorn + Nginx + systemd
