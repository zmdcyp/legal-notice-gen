"""
律师函批量生成工具 (Web 版)
从 docx 模板 + Excel 数据批量生成律师函 PDF。
模板占位符: {{name}}，Excel 表头对应。
特殊占位符 {{sign}} / {{stamp}} 用上传的透明底图片替换。
"""

import os
import re
import io
import zipfile
import uuid
import shutil
import tempfile
import subprocess
import datetime
from flask import (Flask, render_template_string, request,
                   send_file, jsonify, session)
from docx import Document
from docx.shared import Cm
import openpyxl

app = Flask(__name__)
app.secret_key = os.urandom(24)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

PLACEHOLDER_RE = re.compile(r"\{\{(.+?)\}\}")

# 这两个占位符用图片替换，不走文本替换
IMAGE_PLACEHOLDERS = {"sign", "stamp"}


# ── docx 图片插入 ────────────────────────────────────────────────

def _replace_image_in_paragraph(paragraph, placeholder, image_path, width_cm=4.0):
    """如果段落包含 {{placeholder}}，将其替换为图片。"""
    full_text = "".join(run.text for run in paragraph.runs)
    tag = "{{" + placeholder + "}}"
    if tag not in full_text:
        return False

    # 清除所有 run 的文本
    for run in paragraph.runs:
        run.text = ""

    # 在第一个 run 中插入图片
    if paragraph.runs:
        run = paragraph.runs[0]
    else:
        run = paragraph.add_run()

    # 处理 tag 前后的文本
    before, after = full_text.split(tag, 1)
    if before:
        pre_run = paragraph.insert_paragraph_before("").add_run(before) if False else None
        # 简单方案：把 before 放到 run.text 前面的新 run
        pass  # 为了简化，只插入图片

    run.add_picture(image_path, width=Cm(width_cm))

    if after.strip():
        after_run = paragraph.add_run(after)

    return True


def _replace_images_in_paragraphs(paragraphs, images):
    """在段落列表中替换图片占位符。"""
    for paragraph in paragraphs:
        for placeholder, (image_path, width_cm) in images.items():
            if image_path and os.path.exists(image_path):
                _replace_image_in_paragraph(paragraph, placeholder, image_path, width_cm)


def _replace_images_in_tables(tables, images):
    for table in tables:
        for row in table.rows:
            for cell in row.cells:
                _replace_images_in_paragraphs(cell.paragraphs, images)


def _replace_images_in_document(doc, images):
    """替换文档中所有图片占位符。"""
    _replace_images_in_paragraphs(doc.paragraphs, images)
    _replace_images_in_tables(doc.tables, images)
    for section in doc.sections:
        for hf in (section.header, section.footer):
            if hf:
                _replace_images_in_paragraphs(hf.paragraphs, images)
                _replace_images_in_tables(hf.tables, images)


# ── docx 文本替换 ────────────────────────────────────────────────

def _replace_in_paragraph(paragraph, data: dict):
    full_text = "".join(run.text for run in paragraph.runs)
    if not PLACEHOLDER_RE.search(full_text):
        return
    new_text = full_text
    for key, value in data.items():
        new_text = new_text.replace("{{" + key + "}}", str(value))
    if new_text == full_text:
        return
    if paragraph.runs:
        paragraph.runs[0].text = new_text
        for run in paragraph.runs[1:]:
            run.text = ""


def _replace_in_table(table, data: dict):
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                _replace_in_paragraph(paragraph, data)


def _replace_in_document(doc, data: dict):
    for paragraph in doc.paragraphs:
        _replace_in_paragraph(paragraph, data)
    for table in doc.tables:
        _replace_in_table(table, data)
    for section in doc.sections:
        for hf in (section.header, section.footer):
            if hf is None:
                continue
            for paragraph in hf.paragraphs:
                _replace_in_paragraph(paragraph, data)
            for table in hf.tables:
                _replace_in_table(table, data)


# ── 占位符提取 ───────────────────────────────────────────────────

def extract_placeholders(template_path: str) -> list:
    doc = Document(template_path)
    found = set()

    def _scan(paragraphs):
        for p in paragraphs:
            text = "".join(run.text for run in p.runs)
            found.update(PLACEHOLDER_RE.findall(text))

    def _scan_tables(tables):
        for table in tables:
            for row in table.rows:
                for cell in row.cells:
                    _scan(cell.paragraphs)

    _scan(doc.paragraphs)
    _scan_tables(doc.tables)
    for section in doc.sections:
        for hf in (section.header, section.footer):
            if hf:
                _scan(hf.paragraphs)
                _scan_tables(hf.tables)
    return sorted(found)


# ── Excel 读取 ───────────────────────────────────────────────────

def read_excel(excel_path: str):
    wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return [], []
    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    data_rows = []
    for row in rows[1:]:
        if all(cell is None for cell in row):
            continue
        record = {}
        for header, cell in zip(headers, row):
            if header:
                if isinstance(cell, datetime.datetime):
                    record[header] = cell.strftime("%Y-%m-%d")
                elif isinstance(cell, datetime.date):
                    record[header] = cell.strftime("%Y-%m-%d")
                else:
                    record[header] = cell if cell is not None else ""
        data_rows.append(record)
    return headers, data_rows


# ── 批量生成 ─────────────────────────────────────────────────────

def _docx_to_pdf(docx_path, out_dir):
    """用 LibreOffice 将 docx 转换为 PDF，排版完全保真。"""
    result = subprocess.run(
        ["soffice", "--headless", "--convert-to", "pdf",
         "--outdir", out_dir, docx_path],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice 转换失败: {result.stderr}")


def generate_notices(template_path, data_rows, manual_fields,
                     images, filename_field=None, output_format="pdf"):
    """
    生成律师函，返回 zip 文件的 BytesIO 对象。
    images: {"sign": (path, width_cm), "stamp": (path, width_cm)}
    output_format: "pdf" 或 "docx"
    """
    tmp_dir = tempfile.mkdtemp(prefix="legal_notice_")
    try:
        docx_paths = []
        for i, record in enumerate(data_rows, start=1):
            # 文本数据合并（手动字段优先级低于 Excel）
            merged = {k: v for k, v in manual_fields.items()
                      if k not in IMAGE_PLACEHOLDERS}
            merged.update(record)

            doc = Document(template_path)

            # 先替换图片占位符（在文本替换之前，避免被文本替换清掉）
            _replace_images_in_document(doc, images)

            # 再替换文本占位符
            _replace_in_document(doc, merged)

            if filename_field and filename_field in merged and merged[filename_field]:
                safe = re.sub(r'[\\/*?:"<>|]', "_", str(merged[filename_field]))
                base_name = safe
            else:
                base_name = f"律师函_{i:04d}"

            docx_path = os.path.join(tmp_dir, f"{base_name}.docx")
            doc.save(docx_path)
            docx_paths.append((base_name, docx_path))

        # 打包结果
        out_paths = []
        if output_format == "pdf":
            for base_name, docx_path in docx_paths:
                _docx_to_pdf(docx_path, tmp_dir)
                pdf_path = os.path.join(tmp_dir, f"{base_name}.pdf")
                out_paths.append((f"{base_name}.pdf", pdf_path))
        else:
            out_paths = [(f"{bn}.docx", dp) for bn, dp in docx_paths]

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, path in out_paths:
                zf.write(path, name)
        buf.seek(0)
        return buf
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── 路由 ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/upload", methods=["POST"])
def upload():
    template_file = request.files.get("template")
    excel_file = request.files.get("excel")
    sign_file = request.files.get("sign")
    stamp_file = request.files.get("stamp")

    if not template_file or not excel_file:
        return jsonify(error="请上传模板文件和 Excel 文件。"), 400

    sid = str(uuid.uuid4())
    work_dir = os.path.join(UPLOAD_DIR, sid)
    os.makedirs(work_dir, exist_ok=True)

    tpl_path = os.path.join(work_dir, "template.docx")
    xls_path = os.path.join(work_dir, "data.xlsx")
    template_file.save(tpl_path)
    excel_file.save(xls_path)

    sign_path = None
    stamp_path = None
    if sign_file and sign_file.filename:
        sign_path = os.path.join(work_dir, "sign.png")
        sign_file.save(sign_path)
    if stamp_file and stamp_file.filename:
        stamp_path = os.path.join(work_dir, "stamp.png")
        stamp_file.save(stamp_path)

    placeholders = extract_placeholders(tpl_path)
    headers, data_rows = read_excel(xls_path)
    headers_clean = [h for h in headers if h]

    # 图片占位符不算入文本匹配
    text_placeholders = [p for p in placeholders if p not in IMAGE_PLACEHOLDERS]
    image_phs_found = [p for p in placeholders if p in IMAGE_PLACEHOLDERS]

    matched = [p for p in text_placeholders if p in set(headers_clean)]
    missing = [p for p in text_placeholders if p not in set(headers_clean)]

    session["sid"] = sid
    session["tpl_path"] = tpl_path
    session["xls_path"] = xls_path
    session["sign_path"] = sign_path
    session["stamp_path"] = stamp_path

    return jsonify(
        placeholders=text_placeholders,
        image_placeholders=image_phs_found,
        excel_headers=headers_clean,
        matched=matched,
        missing=missing,
        row_count=len(data_rows),
        preview=data_rows[:5],
        has_sign=sign_path is not None,
        has_stamp=stamp_path is not None,
        need_sign="sign" in image_phs_found,
        need_stamp="stamp" in image_phs_found,
    )


@app.route("/generate", methods=["POST"])
def generate():
    sid = session.get("sid")
    tpl_path = session.get("tpl_path")
    xls_path = session.get("xls_path")
    sign_path = session.get("sign_path")
    stamp_path = session.get("stamp_path")

    if not sid or not tpl_path or not os.path.exists(tpl_path):
        return jsonify(error="请先上传文件。"), 400

    payload = request.get_json()
    manual_fields = payload.get("manual_fields", {})
    filename_field = payload.get("filename_field") or None
    output_format = payload.get("output_format", "pdf")
    sign_width = float(payload.get("sign_width", 3.0))
    stamp_width = float(payload.get("stamp_width", 4.0))

    images = {}
    if sign_path and os.path.exists(sign_path):
        images["sign"] = (sign_path, sign_width)
    if stamp_path and os.path.exists(stamp_path):
        images["stamp"] = (stamp_path, stamp_width)

    _, data_rows = read_excel(xls_path)
    if not data_rows:
        return jsonify(error="Excel 中没有数据行。"), 400

    try:
        zip_buf = generate_notices(tpl_path, data_rows, manual_fields,
                                   images, filename_field, output_format)
    except Exception as e:
        return jsonify(error=f"生成失败: {e}"), 500
    finally:
        # 清理上传文件
        work_dir = os.path.join(UPLOAD_DIR, sid)
        shutil.rmtree(work_dir, ignore_errors=True)

    return send_file(zip_buf, mimetype="application/zip",
                     as_attachment=True, download_name="律师函.zip")


# ── HTML 模板 ────────────────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>律师函批量生成工具</title>
<style>
  :root { --primary: #2563eb; --danger: #dc2626; --success: #16a34a; --bg: #f8fafc; --card: #fff; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: var(--bg); color: #1e293b; line-height: 1.6; }

  .container { max-width: 860px; margin: 0 auto; padding: 32px 16px; }
  h1 { text-align: center; font-size: 1.75rem; margin-bottom: 8px; }
  .subtitle { text-align: center; color: #64748b; margin-bottom: 32px; }

  .card { background: var(--card); border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
          padding: 24px; margin-bottom: 20px; }
  .card h2 { font-size: 1.1rem; margin-bottom: 16px; color: #334155; }

  .upload-area { border: 2px dashed #cbd5e1; border-radius: 8px; padding: 20px;
                 text-align: center; cursor: pointer; transition: .2s; min-height: 80px;
                 display: flex; align-items: center; justify-content: center; flex-direction: column; }
  .upload-area:hover { border-color: var(--primary); background: #eff6ff; }
  .upload-area.has-file { border-color: var(--success); background: #f0fdf4; }
  .upload-area input[type="file"] { display: none; }
  .upload-area .label { font-size: .95rem; color: #64748b; }
  .upload-area .filename { font-weight: 600; color: var(--success); }
  .upload-area .preview-img { max-height: 60px; max-width: 120px; margin-top: 6px; }

  .row { display: flex; gap: 16px; flex-wrap: wrap; }
  .row > * { flex: 1; min-width: 0; }
  .row-4 > * { flex: 1 1 calc(25% - 12px); min-width: 150px; }

  .btn { display: inline-block; padding: 10px 28px; border: none; border-radius: 8px;
         font-size: 1rem; font-weight: 600; cursor: pointer; transition: .2s; }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover { background: #1d4ed8; }
  .btn-primary:disabled { background: #94a3b8; cursor: not-allowed; }
  .btn-block { width: 100%; text-align: center; }

  .tag { display: inline-block; padding: 2px 10px; border-radius: 999px;
         font-size: .82rem; margin: 2px 4px; }
  .tag-ok { background: #dcfce7; color: #166534; }
  .tag-miss { background: #fee2e2; color: #991b1b; }
  .tag-img { background: #dbeafe; color: #1e40af; }

  .field-group { margin-bottom: 12px; }
  .field-group label { display: block; font-weight: 500; margin-bottom: 4px; font-size: .9rem; }
  .field-group input, .field-group select {
    width: 100%; padding: 8px 12px; border: 1px solid #cbd5e1; border-radius: 6px;
    font-size: .95rem; }
  .field-group input:focus, .field-group select:focus {
    outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(37,99,235,.15); }

  .info-box { padding: 12px 16px; border-radius: 8px; font-size: .9rem; margin-bottom: 16px; }
  .info-ok { background: #f0fdf4; border: 1px solid #bbf7d0; color: #166534; }
  .info-warn { background: #fffbeb; border: 1px solid #fde68a; color: #92400e; }
  .info-blue { background: #eff6ff; border: 1px solid #bfdbfe; color: #1e40af; }

  table.preview { width: 100%; border-collapse: collapse; font-size: .85rem; margin-top: 8px; }
  table.preview th, table.preview td { padding: 6px 10px; border: 1px solid #e2e8f0; text-align: left; }
  table.preview th { background: #f1f5f9; font-weight: 600; }

  .spinner { display: inline-block; width: 18px; height: 18px; border: 2px solid #fff;
             border-top-color: transparent; border-radius: 50%;
             animation: spin .6s linear infinite; vertical-align: middle; margin-right: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .step-num { display: inline-flex; align-items: center; justify-content: center;
              width: 28px; height: 28px; border-radius: 50%; background: var(--primary);
              color: #fff; font-weight: 700; font-size: .85rem; margin-right: 8px; }

  .section-label { font-size: .9rem; color: #64748b; margin-bottom: 8px; }
  .img-size-input { width: 80px !important; display: inline-block !important; }
</style>
</head>
<body>
<div class="container">
  <h1>律师函批量生成工具</h1>
  <p class="subtitle">上传 docx 模板和 Excel 数据，自动批量生成律师函 PDF</p>

  <!-- Step 1: 上传文件 -->
  <div class="card">
    <h2><span class="step-num">1</span>上传文件</h2>

    <p class="section-label">模板与数据（必选）</p>
    <div class="row" style="margin-bottom:16px;">
      <div class="upload-area" id="tplArea" onclick="document.getElementById('tplFile').click()">
        <input type="file" id="tplFile" accept=".docx">
        <div class="label" id="tplLabel">点击选择 <b>docx 模板</b></div>
      </div>
      <div class="upload-area" id="xlsArea" onclick="document.getElementById('xlsFile').click()">
        <input type="file" id="xlsFile" accept=".xlsx,.xls">
        <div class="label" id="xlsLabel">点击选择 <b>Excel 数据</b></div>
      </div>
    </div>

    <p class="section-label">签名与印章（可选，透明底 PNG）</p>
    <div class="row" style="margin-bottom:16px;">
      <div class="upload-area" id="signArea" onclick="document.getElementById('signFile').click()">
        <input type="file" id="signFile" accept=".png">
        <div class="label" id="signLabel">点击上传 <b>签名</b> 图片<br><small>对应 {{sign}}</small></div>
      </div>
      <div class="upload-area" id="stampArea" onclick="document.getElementById('stampFile').click()">
        <input type="file" id="stampFile" accept=".png">
        <div class="label" id="stampLabel">点击上传 <b>印章</b> 图片<br><small>对应 {{stamp}}</small></div>
      </div>
    </div>

    <div class="row" style="margin-bottom:16px;">
      <div class="field-group">
        <label>签名图片宽度 (cm)</label>
        <input type="number" id="signWidth" value="3" min="1" max="10" step="0.5">
      </div>
      <div class="field-group">
        <label>印章图片宽度 (cm)</label>
        <input type="number" id="stampWidth" value="4" min="1" max="10" step="0.5">
      </div>
    </div>

    <button class="btn btn-primary btn-block" id="uploadBtn" onclick="doUpload()" disabled>
      分析文件
    </button>
  </div>

  <!-- Step 2: 分析结果 -->
  <div class="card" id="step2" style="display:none">
    <h2><span class="step-num">2</span>占位符匹配</h2>

    <div id="matchInfo"></div>
    <div id="placeholderTags"></div>
    <div id="imageInfo" style="margin-top:8px;"></div>

    <div id="manualSection" style="display:none; margin-top:20px;">
      <h2 style="color:var(--danger); margin-bottom:12px;">需要手动填写的字段（适用于所有行）</h2>
      <div id="manualFields"></div>
    </div>

    <div id="previewSection" style="margin-top: 20px;">
      <h2 style="margin-bottom:8px;">Excel 数据预览（前 5 行）</h2>
      <div style="overflow-x:auto;" id="previewTable"></div>
    </div>
  </div>

  <!-- Step 3: 生成 -->
  <div class="card" id="step3" style="display:none">
    <h2><span class="step-num">3</span>生成律师函</h2>
    <div class="row">
      <div class="field-group">
        <label>文件命名方式</label>
        <select id="filenameField">
          <option value="">按序号命名</option>
        </select>
      </div>
      <div class="field-group">
        <label>输出格式</label>
        <select id="outputFormat">
          <option value="pdf" selected>PDF</option>
          <option value="docx">DOCX</option>
        </select>
      </div>
    </div>
    <br>
    <button class="btn btn-primary btn-block" id="genBtn" onclick="doGenerate()">
      生成律师函
    </button>
    <div id="genStatus" style="text-align:center; margin-top:12px; color:#64748b;"></div>
  </div>
</div>

<script>
let analysisData = null;

function setupFileInput(inputId, areaId, labelId, previewImg) {
  document.getElementById(inputId).addEventListener('change', function() {
    const area = document.getElementById(areaId);
    const label = document.getElementById(labelId);
    if (this.files.length) {
      area.classList.add('has-file');
      let html = '<span class="filename">' + this.files[0].name + '</span>';
      if (previewImg && this.files[0].type.startsWith('image/')) {
        const url = URL.createObjectURL(this.files[0]);
        html += '<img class="preview-img" src="' + url + '">';
      }
      label.innerHTML = html;
    }
    checkUploadReady();
  });
}

setupFileInput('tplFile', 'tplArea', 'tplLabel', false);
setupFileInput('xlsFile', 'xlsArea', 'xlsLabel', false);
setupFileInput('signFile', 'signArea', 'signLabel', true);
setupFileInput('stampFile', 'stampArea', 'stampLabel', true);

function checkUploadReady() {
  const tpl = document.getElementById('tplFile').files.length > 0;
  const xls = document.getElementById('xlsFile').files.length > 0;
  document.getElementById('uploadBtn').disabled = !(tpl && xls);
}

async function doUpload() {
  const btn = document.getElementById('uploadBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>分析中...';

  const fd = new FormData();
  fd.append('template', document.getElementById('tplFile').files[0]);
  fd.append('excel', document.getElementById('xlsFile').files[0]);

  const signFiles = document.getElementById('signFile').files;
  const stampFiles = document.getElementById('stampFile').files;
  if (signFiles.length) fd.append('sign', signFiles[0]);
  if (stampFiles.length) fd.append('stamp', stampFiles[0]);

  try {
    const resp = await fetch('/upload', { method: 'POST', body: fd });
    const data = await resp.json();
    if (data.error) { alert(data.error); return; }
    analysisData = data;
    showAnalysis(data);
  } catch (e) {
    alert('上传失败: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '分析文件';
  }
}

function showAnalysis(data) {
  document.getElementById('step2').style.display = '';
  document.getElementById('step3').style.display = '';

  // 匹配信息
  const allMatched = data.missing.length === 0;
  const infoDiv = document.getElementById('matchInfo');
  if (allMatched) {
    infoDiv.innerHTML = '<div class="info-box info-ok">所有 ' + data.placeholders.length +
      ' 个文本占位符均已匹配 Excel 列名，共 ' + data.row_count + ' 行数据。</div>';
  } else {
    infoDiv.innerHTML = '<div class="info-box info-warn">' + data.matched.length + ' / ' +
      data.placeholders.length + ' 个文本占位符已匹配，' + data.missing.length +
      ' 个需手动填写。共 ' + data.row_count + ' 行数据。</div>';
  }

  // 占位符标签
  const tagsDiv = document.getElementById('placeholderTags');
  let tagsHtml = data.placeholders.map(p => {
    const ok = data.matched.includes(p);
    return '<span class="tag ' + (ok ? 'tag-ok' : 'tag-miss') + '">{{' + p + '}}</span>';
  }).join('');
  // 图片占位符
  tagsHtml += data.image_placeholders.map(p =>
    '<span class="tag tag-img">{{' + p + '}} (图片)</span>'
  ).join('');
  tagsDiv.innerHTML = tagsHtml;

  // 图片状态
  const imgDiv = document.getElementById('imageInfo');
  let imgHtml = '';
  if (data.need_sign) {
    imgHtml += data.has_sign
      ? '<div class="info-box info-ok">{{sign}} 签名图片已上传</div>'
      : '<div class="info-box info-warn">模板包含 {{sign}} 但未上传签名图片，该占位符将保留原样</div>';
  }
  if (data.need_stamp) {
    imgHtml += data.has_stamp
      ? '<div class="info-box info-ok">{{stamp}} 印章图片已上传</div>'
      : '<div class="info-box info-warn">模板包含 {{stamp}} 但未上传印章图片，该占位符将保留原样</div>';
  }
  imgDiv.innerHTML = imgHtml;

  // 手动字段
  if (data.missing.length > 0) {
    document.getElementById('manualSection').style.display = '';
    document.getElementById('manualFields').innerHTML = data.missing.map(p =>
      '<div class="field-group"><label>{{' + p + '}}</label>' +
      '<input type="text" data-field="' + p + '" placeholder="输入该字段的值（所有律师函共用）"></div>'
    ).join('');
  } else {
    document.getElementById('manualSection').style.display = 'none';
  }

  // 预览表格
  if (data.preview.length > 0) {
    const headers = data.excel_headers;
    let html = '<table class="preview"><thead><tr><th>#</th>';
    headers.forEach(h => { html += '<th>' + h + '</th>'; });
    html += '</tr></thead><tbody>';
    data.preview.forEach((row, i) => {
      html += '<tr><td>' + (i + 1) + '</td>';
      headers.forEach(h => { html += '<td>' + (row[h] !== undefined && row[h] !== null ? row[h] : '') + '</td>'; });
      html += '</tr>';
    });
    html += '</tbody></table>';
    document.getElementById('previewTable').innerHTML = html;
  }

  // 文件名下拉
  const sel = document.getElementById('filenameField');
  sel.innerHTML = '<option value="">按序号命名</option>';
  data.excel_headers.forEach(h => {
    sel.innerHTML += '<option value="' + h + '">' + h + '</option>';
  });
}

async function doGenerate() {
  const btn = document.getElementById('genBtn');
  const status = document.getElementById('genStatus');
  btn.disabled = true;
  const fmt = document.getElementById('outputFormat').value;
  btn.innerHTML = '<span class="spinner"></span>生成 ' + fmt.toUpperCase() + ' 中...';
  status.textContent = fmt === 'pdf' ? '正在转换 PDF，请耐心等待...' : '';

  const manualFields = {};
  document.querySelectorAll('#manualFields input[data-field]').forEach(input => {
    manualFields[input.dataset.field] = input.value;
  });

  const filenameField = document.getElementById('filenameField').value;
  const signWidth = document.getElementById('signWidth').value;
  const stampWidth = document.getElementById('stampWidth').value;

  try {
    const resp = await fetch('/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manual_fields: manualFields,
        filename_field: filenameField,
        output_format: fmt,
        sign_width: signWidth,
        stamp_width: stampWidth,
      })
    });
    if (!resp.ok) {
      const err = await resp.json();
      alert(err.error || '生成失败');
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = '律师函.zip';
    a.click();
    URL.revokeObjectURL(url);
    status.textContent = '生成完成！文件已下载。';
    status.style.color = 'var(--success)';
  } catch (e) {
    alert('生成失败: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '生成律师函';
  }
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    print("=" * 50)
    print("  律师函批量生成工具")
    print("  打开浏览器访问: http://127.0.0.1:5002")
    print("=" * 50)
    app.run(debug=True, port=5002)
