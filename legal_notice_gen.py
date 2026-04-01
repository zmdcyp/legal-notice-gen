"""
Legal Notice Batch Generator (Web)
Generate legal notices from a docx template + Excel data.
Placeholders in template: {{name}}, matched by Excel column headers.
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
import openpyxl

app = Flask(__name__)
app.secret_key = os.urandom(24)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

PLACEHOLDER_RE = re.compile(r"\{\{(.+?)\}\}")


# ── docx text replacement ───────────────────────────────────────

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


# ── placeholder extraction ───────────────────────────────────────

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


# ── Excel reading ────────────────────────────────────────────────

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


# ── batch generation ─────────────────────────────────────────────

def _docx_to_pdf(docx_path, out_dir):
    """Convert docx to PDF using LibreOffice (layout-accurate)."""
    result = subprocess.run(
        ["soffice", "--headless", "--convert-to", "pdf",
         "--outdir", out_dir, docx_path],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice conversion failed: {result.stderr}")


def generate_notices(template_path, data_rows, manual_fields,
                     filename_field=None, output_format="pdf"):
    """Generate legal notices, return a zip BytesIO."""
    tmp_dir = tempfile.mkdtemp(prefix="legal_notice_")
    try:
        docx_paths = []
        for i, record in enumerate(data_rows, start=1):
            merged = {**manual_fields, **record}

            doc = Document(template_path)
            _replace_in_document(doc, merged)

            if filename_field and filename_field in merged and merged[filename_field]:
                safe = re.sub(r'[\\/*?:"<>|]', "_", str(merged[filename_field]))
                base_name = safe
            else:
                base_name = f"notice_{i:04d}"

            docx_path = os.path.join(tmp_dir, f"{base_name}.docx")
            doc.save(docx_path)
            docx_paths.append((base_name, docx_path))

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


# ── routes ───────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/upload", methods=["POST"])
def upload():
    template_file = request.files.get("template")
    excel_file = request.files.get("excel")

    if not template_file or not excel_file:
        return jsonify(error="Please upload both template and Excel files."), 400

    sid = str(uuid.uuid4())
    work_dir = os.path.join(UPLOAD_DIR, sid)
    os.makedirs(work_dir, exist_ok=True)

    tpl_path = os.path.join(work_dir, "template.docx")
    xls_path = os.path.join(work_dir, "data.xlsx")
    template_file.save(tpl_path)
    excel_file.save(xls_path)

    placeholders = extract_placeholders(tpl_path)
    headers, data_rows = read_excel(xls_path)
    headers_clean = [h for h in headers if h]

    matched = [p for p in placeholders if p in set(headers_clean)]
    missing = [p for p in placeholders if p not in set(headers_clean)]

    session["sid"] = sid
    session["tpl_path"] = tpl_path
    session["xls_path"] = xls_path

    return jsonify(
        placeholders=placeholders,
        excel_headers=headers_clean,
        matched=matched,
        missing=missing,
        row_count=len(data_rows),
        preview=data_rows[:5],
    )


@app.route("/generate", methods=["POST"])
def generate():
    sid = session.get("sid")
    tpl_path = session.get("tpl_path")
    xls_path = session.get("xls_path")

    if not sid or not tpl_path or not os.path.exists(tpl_path):
        return jsonify(error="Please upload files first."), 400

    payload = request.get_json()
    manual_fields = payload.get("manual_fields", {})
    filename_field = payload.get("filename_field") or None
    output_format = payload.get("output_format", "pdf")

    _, data_rows = read_excel(xls_path)
    if not data_rows:
        return jsonify(error="No data rows found in Excel."), 400

    try:
        zip_buf = generate_notices(tpl_path, data_rows, manual_fields,
                                   filename_field, output_format)
    except Exception as e:
        return jsonify(error=f"Generation failed: {e}"), 500
    finally:
        work_dir = os.path.join(UPLOAD_DIR, sid)
        shutil.rmtree(work_dir, ignore_errors=True)

    return send_file(zip_buf, mimetype="application/zip",
                     as_attachment=True, download_name="legal_notices.zip")


# ── HTML template ────────────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Legal Notice Generator</title>
<style>
  :root { --primary: #2563eb; --danger: #dc2626; --success: #16a34a; --bg: #f8fafc; --card: #fff; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", Roboto, "PingFang SC", sans-serif;
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

  .row { display: flex; gap: 16px; flex-wrap: wrap; }
  .row > * { flex: 1; min-width: 0; }

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
</style>
</head>
<body>
<div class="container">
  <h1>Legal Notice Generator</h1>
  <p class="subtitle">Upload a docx template and Excel data to batch-generate legal notices</p>

  <!-- Step 1: Upload -->
  <div class="card">
    <h2><span class="step-num">1</span>Upload Files</h2>
    <div class="row" style="margin-bottom:16px;">
      <div class="upload-area" id="tplArea" onclick="document.getElementById('tplFile').click()">
        <input type="file" id="tplFile" accept=".docx">
        <div class="label" id="tplLabel">Click to select <b>docx template</b></div>
      </div>
      <div class="upload-area" id="xlsArea" onclick="document.getElementById('xlsFile').click()">
        <input type="file" id="xlsFile" accept=".xlsx,.xls">
        <div class="label" id="xlsLabel">Click to select <b>Excel data</b></div>
      </div>
    </div>
    <button class="btn btn-primary btn-block" id="uploadBtn" onclick="doUpload()" disabled>
      Analyze Files
    </button>
  </div>

  <!-- Step 2: Analysis -->
  <div class="card" id="step2" style="display:none">
    <h2><span class="step-num">2</span>Placeholder Matching</h2>

    <div id="matchInfo"></div>
    <div id="placeholderTags"></div>

    <div id="manualSection" style="display:none; margin-top:20px;">
      <h2 style="color:var(--danger); margin-bottom:12px;">Fields requiring manual input (applied to all rows)</h2>
      <div id="manualFields"></div>
    </div>

    <div id="previewSection" style="margin-top: 20px;">
      <h2 style="margin-bottom:8px;">Excel Data Preview (first 5 rows)</h2>
      <div style="overflow-x:auto;" id="previewTable"></div>
    </div>
  </div>

  <!-- Step 3: Generate -->
  <div class="card" id="step3" style="display:none">
    <h2><span class="step-num">3</span>Generate Notices</h2>
    <div class="row">
      <div class="field-group">
        <label>File Naming</label>
        <select id="filenameField">
          <option value="">Sequential numbering</option>
        </select>
      </div>
      <div class="field-group">
        <label>Output Format</label>
        <select id="outputFormat">
          <option value="pdf" selected>PDF</option>
          <option value="docx">DOCX</option>
        </select>
      </div>
    </div>
    <br>
    <button class="btn btn-primary btn-block" id="genBtn" onclick="doGenerate()">
      Generate Notices
    </button>
    <div id="genStatus" style="text-align:center; margin-top:12px; color:#64748b;"></div>
  </div>
</div>

<script>
let analysisData = null;

function setupFileInput(inputId, areaId, labelId) {
  document.getElementById(inputId).addEventListener('change', function() {
    const area = document.getElementById(areaId);
    const label = document.getElementById(labelId);
    if (this.files.length) {
      area.classList.add('has-file');
      label.innerHTML = '<span class="filename">' + this.files[0].name + '</span>';
    }
    checkUploadReady();
  });
}

setupFileInput('tplFile', 'tplArea', 'tplLabel');
setupFileInput('xlsFile', 'xlsArea', 'xlsLabel');

function checkUploadReady() {
  const tpl = document.getElementById('tplFile').files.length > 0;
  const xls = document.getElementById('xlsFile').files.length > 0;
  document.getElementById('uploadBtn').disabled = !(tpl && xls);
}

async function doUpload() {
  const btn = document.getElementById('uploadBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Analyzing...';

  const fd = new FormData();
  fd.append('template', document.getElementById('tplFile').files[0]);
  fd.append('excel', document.getElementById('xlsFile').files[0]);

  try {
    const resp = await fetch('/upload', { method: 'POST', body: fd });
    const data = await resp.json();
    if (data.error) { alert(data.error); return; }
    analysisData = data;
    showAnalysis(data);
  } catch (e) {
    alert('Upload failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Analyze Files';
  }
}

function showAnalysis(data) {
  document.getElementById('step2').style.display = '';
  document.getElementById('step3').style.display = '';

  const allMatched = data.missing.length === 0;
  const infoDiv = document.getElementById('matchInfo');
  if (allMatched) {
    infoDiv.innerHTML = '<div class="info-box info-ok">All ' + data.placeholders.length +
      ' placeholders matched with Excel columns. ' + data.row_count + ' data rows found.</div>';
  } else {
    infoDiv.innerHTML = '<div class="info-box info-warn">' + data.matched.length + ' / ' +
      data.placeholders.length + ' placeholders matched, ' + data.missing.length +
      ' require manual input. ' + data.row_count + ' data rows found.</div>';
  }

  const tagsDiv = document.getElementById('placeholderTags');
  tagsDiv.innerHTML = data.placeholders.map(p => {
    const ok = data.matched.includes(p);
    return '<span class="tag ' + (ok ? 'tag-ok' : 'tag-miss') + '">{{' + p + '}}</span>';
  }).join('');

  if (data.missing.length > 0) {
    document.getElementById('manualSection').style.display = '';
    document.getElementById('manualFields').innerHTML = data.missing.map(p =>
      '<div class="field-group"><label>{{' + p + '}}</label>' +
      '<input type="text" data-field="' + p + '" placeholder="Enter value (shared across all notices)"></div>'
    ).join('');
  } else {
    document.getElementById('manualSection').style.display = 'none';
  }

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

  const sel = document.getElementById('filenameField');
  sel.innerHTML = '<option value="">Sequential numbering</option>';
  data.excel_headers.forEach(h => {
    sel.innerHTML += '<option value="' + h + '">' + h + '</option>';
  });
}

async function doGenerate() {
  const btn = document.getElementById('genBtn');
  const status = document.getElementById('genStatus');
  btn.disabled = true;
  const fmt = document.getElementById('outputFormat').value;
  btn.innerHTML = '<span class="spinner"></span>Generating ' + fmt.toUpperCase() + '...';
  status.textContent = fmt === 'pdf' ? 'Converting to PDF, please wait...' : '';

  const manualFields = {};
  document.querySelectorAll('#manualFields input[data-field]').forEach(input => {
    manualFields[input.dataset.field] = input.value;
  });

  const filenameField = document.getElementById('filenameField').value;

  try {
    const resp = await fetch('/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manual_fields: manualFields,
        filename_field: filenameField,
        output_format: fmt,
      })
    });
    if (!resp.ok) {
      const err = await resp.json();
      alert(err.error || 'Generation failed');
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'legal_notices.zip';
    a.click();
    URL.revokeObjectURL(url);
    status.textContent = 'Done! File downloaded.';
    status.style.color = 'var(--success)';
  } catch (e) {
    alert('Generation failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Generate Notices';
  }
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    print("=" * 50)
    print("  Legal Notice Generator")
    print("  Open browser: http://127.0.0.1:5002")
    print("=" * 50)
    app.run(debug=True, port=5002)
