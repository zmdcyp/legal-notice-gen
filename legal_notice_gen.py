"""
Legal Notice Batch Generator (Web)

Placeholder-neutral batch processor:
- The tool only replaces {{placeholder}} tokens in a docx template with
  values from Excel columns whose headers match the placeholder names.
- It does NOT interpret, validate, or understand the business content of
  the template or spreadsheet. Any docx + any Excel that share matching
  field names will work.

Scale-oriented architecture:
- Async task model: /generate returns a task_id immediately; the client
  polls /status and fetches the final zip from /download. Avoids HTTP
  request-level timeouts (Nginx/Gunicorn) on long batches.
- Parallel docx rendering via ThreadPoolExecutor.
- Parallel PDF conversion via multiple soffice workers, each with its own
  UserInstallation profile dir; files are batched per worker so soffice
  starts only N times rather than once per file.

Single-file delivery:
- Everything (backend, HTML, CSS, JS) is in this one file. Ship by copying
  legal_notice_gen.py + requirements.txt to the target machine.
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
import threading
import traceback
import concurrent.futures
import time
from collections import defaultdict

from flask import (Flask, request, send_file, jsonify, session,
                   Response, after_this_request, redirect)
import hmac as _hmac
from docx import Document
import openpyxl

# ── app setup ───────────────────────────────────────────────────

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Persist secret_key across restarts so /upload -> /generate sessions survive.
_SECRET_FILE = os.path.join(UPLOAD_DIR, ".secret_key")
if os.path.exists(_SECRET_FILE):
    with open(_SECRET_FILE, "rb") as _f:
        app.secret_key = _f.read()
else:
    app.secret_key = os.urandom(32)
    with open(_SECRET_FILE, "wb") as _f:
        _f.write(app.secret_key)

PLACEHOLDER_RE = re.compile(r"\{\{(.+?)\}\}")
SAFE_NAME_RE = re.compile(r'[\\/*?:"<>|\r\n\t]+')

# ── access auth ─────────────────────────────────────────────────
#
# Single-password gate. Override at runtime with the LEGAL_NOTICE_PASSWORD
# environment variable (strongly recommended in production). The default
# baked into the file is a randomly generated 20-character password; change
# it by setting the env var on the VPS/systemd unit.
_DEFAULT_PASSWORD = "RT%L6IXoXT*^r=z6%npe"
APP_PASSWORD = os.environ.get("LEGAL_NOTICE_PASSWORD") or _DEFAULT_PASSWORD

# Paths that never require auth (the login form itself + logout).
_AUTH_EXEMPT_PATHS = {"/login", "/logout"}

# API endpoints return JSON 401 instead of redirecting, so the fetch() calls
# on the front-end don't silently follow a redirect to HTML.
_AUTH_API_PREFIXES = ("/upload", "/generate", "/status", "/download")

# ── machine profiles ────────────────────────────────────────────
#
# Concurrency used to be a single module constant. But this project runs in
# two very different environments:
#   1. Locally on a powerful Mac for big interactive batches.
#   2. On a small VPS for always-on access.
# The webpage lets the user pick which profile to use at generate time. The
# default is conservative (VPS); picking "mac" unlocks process-pool-based
# rendering that scales linearly with CPU cores — the real speedup lever for
# DOCX output, which is this project's primary mode.
_CPU = os.cpu_count() or 4

MACHINE_PROFILES = {
    "vps": {
        "render_workers":  2,
        "convert_workers": 2,
        "pdf_chunk_size":  15,
        "label": "VPS (conservative)",
    },
    "mac": {
        "render_workers":  _CPU,
        "convert_workers": _CPU,
        "pdf_chunk_size":  10,
        "label": "Mac (max perf)",
    },
}
DEFAULT_MACHINE = "vps"


def _get_machine_profile(name):
    return MACHINE_PROFILES.get((name or "").lower()) or MACHINE_PROFILES[DEFAULT_MACHINE]


# soffice timeout: generous per-file allowance with a floor so small batches
# don't time out prematurely.
SOFFICE_TIMEOUT_PER_FILE = 15
SOFFICE_TIMEOUT_MIN = 180


# ── task registry ────────────────────────────────────────────────

# In-process task store. REQUIRES Gunicorn --workers 1 --threads N so that
# /status and /download reach the same process that started the task.
TASKS = {}
TASKS_LOCK = threading.Lock()
TASK_TTL_SECONDS = 3600  # prune finished/stale tasks after 1 hour


def _new_task():
    _prune_tasks()
    tid = uuid.uuid4().hex
    with TASKS_LOCK:
        TASKS[tid] = {
            "status": "pending",     # pending | running | done | error
            "stage": "queued",       # queued | rendering | converting | packing | done
            "progress": 0,
            "total": 0,
            "message": "",
            "result_path": None,
            "error": None,
            "created": time.time(),
        }
    return tid


def _update_task(tid, **fields):
    with TASKS_LOCK:
        if tid in TASKS:
            TASKS[tid].update(fields)


def _get_task(tid):
    with TASKS_LOCK:
        task = TASKS.get(tid)
        return dict(task) if task else None


def _drop_task(tid):
    with TASKS_LOCK:
        return TASKS.pop(tid, None)


def _prune_tasks():
    """Remove old finished tasks and their leftover part files."""
    cutoff = time.time() - TASK_TTL_SECONDS
    stale_paths = []
    with TASKS_LOCK:
        for tid, t in list(TASKS.items()):
            if t["created"] < cutoff and t["status"] in ("done", "error"):
                for p in (t.get("ready_parts") or []):
                    if p.get("path"):
                        stale_paths.append(p["path"])
                TASKS.pop(tid, None)
    for path in stale_paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


# ── docx text replacement ───────────────────────────────────────

def _replace_in_paragraph(paragraph, data: dict):
    full_text = "".join(run.text for run in paragraph.runs)
    if not PLACEHOLDER_RE.search(full_text):
        return
    new_text = full_text
    for key, value in data.items():
        new_text = new_text.replace("{{" + key + "}}", _format_value(value))
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


def _format_value(v):
    """Stringify a cell value for substitution.

    - Integer-valued floats become plain integers so IDs / CNICs / phone
      numbers don't render as '1234.0'.
    - Non-integer floats are treated as monetary amounts and rendered with
      thousands separators and 2 decimal places ('1234.5' -> '1,234.50').
    - Money columns are usually formatted at read_excel() time based on the
      column header (see _is_money_header) — by the time a value reaches
      this function it's already a pre-formatted string in that case.
    """
    if v is None:
        return ""
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return f"{v:,.2f}"
    return str(v)


# ── money column detection ───────────────────────────────────────
#
# The user's real workload has columns like Principal_Amount, Interest,
# Penalty, Payable that are stored in Excel as integer-valued numbers
# (e.g. 500000, not 500000.00). The integer-valued-float fast path in
# _format_value would drop them to plain integers with no commas. To get
# the formal "1,234.56" style the user wants, we match money columns by
# header name and always format them as {value:,.2f}, regardless of
# whether the cell was stored as int or float.
#
# The tool is still placeholder-neutral: this list is a generic set of
# money words in English and Chinese. If your template uses a money
# column not covered here, add the keyword below.

MONEY_KEYWORDS = (
    # English
    "amount", "interest", "penalty", "payable", "fee", "balance",
    "total", "principal", "charge", "payment", "debt", "price", "cost",
    # Chinese
    "金额", "利息", "罚息", "罚金", "应付", "应还", "应缴", "费用",
    "总额", "本金", "欠款", "滞纳金", "款项",
)


def _is_money_header(header):
    """Return True if the column header looks like a monetary field."""
    h = (header or "").strip().lower()
    if not h:
        return False
    # Never treat explicit date columns as money, even if another money
    # keyword happens to appear elsewhere in the string.
    if h == "date" or h.endswith("_date") or h.endswith(" date"):
        return False
    return any(kw in h for kw in MONEY_KEYWORDS)


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
            if not header:
                continue
            if isinstance(cell, (datetime.datetime, datetime.date)):
                record[header] = cell.strftime("%d/%m/%Y")
            elif _is_money_header(header) and isinstance(cell, (int, float)) and not isinstance(cell, bool):
                # Money columns are formatted up-front to "1,234.56" style
                # regardless of whether Excel stored the value as int or
                # float — integer-valued amounts (500000) and float
                # amounts (500000.5) both land in the same formal shape.
                record[header] = f"{float(cell):,.2f}"
            elif isinstance(cell, float) and cell.is_integer():
                # Integer-valued numeric cells (CNIC, phone numbers, etc.)
                # should not carry a trailing .0 into the notice.
                record[header] = int(cell)
            else:
                record[header] = cell if cell is not None else ""
        data_rows.append(record)
    return headers, data_rows


# ── filename helpers ─────────────────────────────────────────────

def _safe_name(value) -> str:
    s = _format_value(value)
    s = SAFE_NAME_RE.sub("_", s).strip().strip(".")
    return s or "unnamed"


def _build_filename(record, filename_fields, idx):
    """Join the selected field values with '_' to form the base filename."""
    if filename_fields:
        parts = []
        for f in filename_fields:
            v = record.get(f, "")
            if v != "" and v is not None:
                parts.append(_format_value(v))
        if parts:
            return _safe_name("_".join(parts))
    return f"notice_{idx:04d}"


# ── render worker (module-scope so ProcessPoolExecutor can pickle it) ──

def _render_one_job(args):
    """Open the template, replace placeholders, save the output docx.

    Must be a plain module-scope function so `ProcessPoolExecutor` can
    pickle it for cross-process dispatch. Returns the output path on
    success and raises on failure — callers rely on `as_completed` for
    progress counting, not on a return value.

    `args` is a tuple `(template_path, merged, docx_path)` — all plain,
    picklable types (strings and a flat dict).
    """
    template_path, merged, docx_path = args
    doc = Document(template_path)
    _replace_in_document(doc, merged)
    doc.save(docx_path)
    return docx_path


# ── batched LibreOffice PDF conversion ───────────────────────────

def _batch_docx_to_pdf(docx_paths, out_dir, worker_id):
    """Convert a batch of docx files to PDF in a single soffice invocation.

    Each worker uses its own UserInstallation profile directory, which is
    the only reliable way to run multiple soffice processes in parallel
    without profile-lock contention.
    """
    if not docx_paths:
        return
    profile_dir = tempfile.mkdtemp(prefix=f"soffice_profile_{worker_id}_")
    try:
        timeout = max(SOFFICE_TIMEOUT_MIN,
                      len(docx_paths) * SOFFICE_TIMEOUT_PER_FILE)
        result = subprocess.run(
            ["soffice",
             f"-env:UserInstallation=file://{profile_dir}",
             "--headless", "--convert-to", "pdf",
             "--outdir", out_dir, *docx_paths],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"LibreOffice conversion failed (worker {worker_id}): "
                f"{result.stderr[:500]}")
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


# ── group-by-group generation ────────────────────────────────────

def _process_group(template_path, rows, group_name, manual_fields,
                   filename_fields, output_format, task_id,
                   cumulative_before, grand_total,
                   render_pool, render_mode,
                   convert_workers, pdf_chunk_size):
    """Render, convert (if PDF), and zip up one group's notices.

    Returns the on-disk path of the group's zip. The zip contains a flat
    list of files named by `filename_fields` (joined with `_`).

    `render_pool` is a long-lived executor (process or thread) provided by
    the caller; this keeps the process-spawn cost amortized across all
    groups in the task instead of paying it per group. `render_mode` is
    either "processes" or "threads" for user-visible logging.
    """
    tmp_dir = tempfile.mkdtemp(prefix=f"notice_{_safe_name(group_name)[:32]}_")
    try:
        docx_dir = os.path.join(tmp_dir, "docx")
        pdf_dir = os.path.join(tmp_dir, "pdf")
        os.makedirs(docx_dir, exist_ok=True)
        os.makedirs(pdf_dir, exist_ok=True)

        # ── build per-row jobs with filename uniqueness inside this group ──
        taken = set()
        jobs = []
        for i, record in enumerate(rows, start=1):
            merged = {**manual_fields, **record}
            base = _build_filename(merged, filename_fields, i)
            unique = base
            n = 1
            while unique in taken:
                n += 1
                unique = f"{base}_{n}"
            taken.add(unique)
            internal = uuid.uuid4().hex
            jobs.append({
                "base": unique,
                "internal": internal,
                "docx_path": os.path.join(docx_dir, f"{internal}.docx"),
                "merged": merged,
            })

        group_total = len(jobs)
        progress_lock = threading.Lock()

        def bump(stage_label, group_done):
            """Update both per-group and overall progress counters."""
            if not task_id:
                return
            _update_task(
                task_id,
                stage=stage_label,
                group_progress=group_done,
                progress=cumulative_before + group_done,
                message=f"{stage_label} {group_name}: {group_done}/{group_total}",
            )

        # ── parallel docx rendering ──
        if task_id:
            _update_task(task_id, stage="rendering",
                         current_group=group_name,
                         group_total=group_total,
                         group_progress=0,
                         message=f"rendering {group_name} via {render_mode}: 0/{group_total}")

        # Submit all jobs to the caller-provided render pool. The pool may
        # be a ProcessPoolExecutor (GIL-free, scales with cores) or a
        # ThreadPoolExecutor fallback — the `_render_one_job` function is
        # the same either way. Progress ticks come from `as_completed`,
        # counted on the main side, so no cross-process shared state is
        # needed.
        payloads = [(template_path, job["merged"], job["docx_path"]) for job in jobs]
        futures = [render_pool.submit(_render_one_job, p) for p in payloads]
        done_count = 0
        for fut in concurrent.futures.as_completed(futures):
            fut.result()  # propagate exceptions
            done_count += 1
            if done_count % 5 == 0 or done_count == group_total:
                bump("rendering", done_count)
        bump("rendering", group_total)

        # ── conversion (optional) ──
        out_items = []  # list of (arcname_in_zip, disk_path)
        if output_format == "pdf":
            if task_id:
                _update_task(task_id, stage="converting",
                             group_progress=0,
                             message=f"converting {group_name}: 0/{group_total}")

            docx_paths = [j["docx_path"] for j in jobs]
            chunks = [docx_paths[i:i + pdf_chunk_size]
                      for i in range(0, len(docx_paths), pdf_chunk_size)]

            converted = {"n": 0}
            worker_seq = {"n": 0}

            def convert_chunk(chunk):
                # Unique worker id per chunk so each soffice call gets its
                # own UserInstallation profile dir.
                with progress_lock:
                    worker_seq["n"] += 1
                    wid = worker_seq["n"]
                _batch_docx_to_pdf(chunk, pdf_dir, wid)
                with progress_lock:
                    converted["n"] += len(chunk)
                    n = converted["n"]
                bump("converting", n)

            n_workers = min(convert_workers, max(1, len(chunks)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
                for _ in pool.map(convert_chunk, chunks):
                    pass
            bump("converting", group_total)

            for job in jobs:
                pdf_path = os.path.join(pdf_dir, f"{job['internal']}.pdf")
                if not os.path.exists(pdf_path):
                    raise RuntimeError(f"Missing PDF output for {job['base']}")
                out_items.append((f"{job['base']}.pdf", pdf_path))
        else:
            for job in jobs:
                out_items.append((f"{job['base']}.docx", job["docx_path"]))

        # ── pack this group into its own zip ──
        if task_id:
            _update_task(task_id, stage="packing",
                         message=f"packing {group_name}.zip")

        safe_group = _safe_name(group_name)
        zip_path = os.path.join(
            UPLOAD_DIR, f"part_{uuid.uuid4().hex}_{safe_group}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, path in out_items:
                zf.write(path, arcname)
        return zip_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def generate_notices(template_path, data_rows, manual_fields,
                     filename_fields=None, group_by_field=None,
                     output_format="docx", task_id=None,
                     render_workers=2, convert_workers=2,
                     pdf_chunk_size=15, profile_label=""):
    """Group-by-group processing.

    - Sort rows by `group_by_field` (stable), bucket into ordered groups.
    - For each group: render → convert → pack into `<group>.zip`.
    - As soon as a group's zip is ready, append it to the task's
      `ready_parts` list; the frontend polls for new parts and downloads
      each one immediately. There is no outer "master" zip.
    - If `group_by_field` is empty, the whole batch is treated as a single
      group named "output" and produces a single zip.

    The render pool is created once per task and reused across all groups
    to amortize process-spawn cost. If `ProcessPoolExecutor` can't be
    constructed for any reason, we fall back to `ThreadPoolExecutor` —
    slower but guaranteed to work anywhere.
    """
    total = len(data_rows)

    if group_by_field:
        def group_key(r):
            return _format_value(r.get(group_by_field, ""))
        sorted_rows = sorted(data_rows, key=group_key)
        buckets = defaultdict(list)
        for r in sorted_rows:
            g = _safe_name(r.get(group_by_field, "") or "unassigned")
            buckets[g].append(r)
        groups_ordered = list(buckets.items())
    else:
        groups_ordered = [("output", list(data_rows))]

    # Build the long-lived render pool once per task. Try processes first
    # (GIL-free, scales with cores). Fall back to threads on any startup
    # error so the tool still works on constrained environments.
    render_pool = None
    render_mode = "processes"
    try:
        render_pool = concurrent.futures.ProcessPoolExecutor(max_workers=render_workers)
    except (OSError, ImportError, NotImplementedError) as e:
        print(f"[generate_notices] ProcessPoolExecutor unavailable ({e}); "
              f"falling back to threads")
        render_pool = concurrent.futures.ThreadPoolExecutor(max_workers=render_workers)
        render_mode = "threads"

    start_message = (f"Processing {len(groups_ordered)} group(s), "
                     f"{total} item(s) total · profile={profile_label or '?'} "
                     f"· render={render_mode}×{render_workers} "
                     f"· convert={convert_workers}")

    if task_id:
        _update_task(task_id,
                     status="running",
                     stage="queued",
                     total=total,
                     progress=0,
                     groups_total=len(groups_ordered),
                     groups_done=0,
                     ready_parts=[],
                     current_group="",
                     group_total=0,
                     group_progress=0,
                     message=start_message)

    cumulative = 0
    try:
        for idx, (group_name, rows) in enumerate(groups_ordered):
            if task_id:
                _update_task(task_id, current_group=group_name)
            zip_path = _process_group(
                template_path=template_path,
                rows=rows,
                group_name=group_name,
                manual_fields=manual_fields,
                filename_fields=filename_fields,
                output_format=output_format,
                task_id=task_id,
                cumulative_before=cumulative,
                grand_total=total,
                render_pool=render_pool,
                render_mode=render_mode,
                convert_workers=convert_workers,
                pdf_chunk_size=pdf_chunk_size,
            )
            cumulative += len(rows)

            # Append this group's zip to ready_parts so the frontend can pick
            # it up on its next status poll.
            with TASKS_LOCK:
                t = TASKS.get(task_id) if task_id else None
                if t is not None:
                    parts = t.get("ready_parts") or []
                    parts.append({
                        "index": len(parts),
                        "name": f"{_safe_name(group_name)}.zip",
                        "group": group_name,
                        "path": zip_path,
                    })
                    t["ready_parts"] = parts
                    t["groups_done"] = idx + 1
                    t["progress"] = cumulative
    finally:
        # Always shut the render pool down, even on exception, so worker
        # processes don't leak across tasks.
        render_pool.shutdown(wait=True)

    if task_id:
        _update_task(task_id, status="done", stage="done",
                     progress=total,
                     message=f"All {len(groups_ordered)} group(s) ready")


# ── routes ───────────────────────────────────────────────────────

@app.before_request
def _require_auth():
    if request.path in _AUTH_EXEMPT_PATHS:
        return None
    if session.get("auth_ok") is True:
        return None
    # API endpoints: JSON 401 so the front-end can react without following
    # a redirect into HTML and breaking its fetch().then(r=>r.json()) chain.
    if request.path.startswith(_AUTH_API_PREFIXES):
        return jsonify(error="auth required"), 401
    # Everything else (HTML pages): bounce to the login form.
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        supplied = (request.form.get("password") or "").encode("utf-8")
        expected = APP_PASSWORD.encode("utf-8")
        if _hmac.compare_digest(supplied, expected):
            session["auth_ok"] = True
            session.permanent = True
            return redirect("/")
        return Response(
            LOGIN_TEMPLATE.replace("__ERROR__",
                '<div class="error">Incorrect password.</div>'),
            mimetype="text/html", status=401)
    return Response(
        LOGIN_TEMPLATE.replace("__ERROR__", ""),
        mimetype="text/html")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.pop("auth_ok", None)
    session.pop("sid", None)
    session.pop("tpl_path", None)
    session.pop("xls_path", None)
    return redirect("/login")


@app.route("/")
def index():
    # Bypass Jinja: the inline HTML template contains literal `{{...}}`
    # placeholder syntax inside JavaScript strings that would otherwise
    # collide with Jinja's expression delimiters.
    return Response(HTML_TEMPLATE, mimetype="text/html")


@app.route("/upload", methods=["POST"])
def upload():
    template_file = request.files.get("template")
    excel_file = request.files.get("excel")

    if not template_file or not excel_file:
        return jsonify(error="Please upload both template and Excel files."), 400

    sid = uuid.uuid4().hex
    work_dir = os.path.join(UPLOAD_DIR, sid)
    os.makedirs(work_dir, exist_ok=True)

    tpl_path = os.path.join(work_dir, "template.docx")
    xls_path = os.path.join(work_dir, "data.xlsx")
    template_file.save(tpl_path)
    excel_file.save(xls_path)

    try:
        placeholders = extract_placeholders(tpl_path)
        headers, data_rows = read_excel(xls_path)
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify(error=f"Failed to read files: {e}"), 400

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

    payload = request.get_json() or {}
    manual_fields = payload.get("manual_fields", {}) or {}
    filename_fields = payload.get("filename_fields") or []
    group_by_field = payload.get("group_by_field") or None
    output_format = payload.get("output_format", "docx")

    # Machine profile is chosen in the webpage. Default is the conservative
    # "vps" profile. Picking "mac" unlocks all cores for ProcessPoolExecutor.
    profile = _get_machine_profile(payload.get("machine"))

    _, data_rows = read_excel(xls_path)
    if not data_rows:
        return jsonify(error="No data rows found in Excel."), 400

    # Date is picked in the webpage and always overrides any Excel column.
    # The webpage sends an ISO date string (YYYY-MM-DD); we format as DD/MM/YYYY
    # and force it onto every row's "date" field, stomping whatever Excel had.
    date_value = (payload.get("date_value") or "").strip()
    if date_value:
        try:
            d = datetime.date.fromisoformat(date_value)
            formatted = d.strftime("%d/%m/%Y")
            for r in data_rows:
                r["date"] = formatted
        except ValueError:
            pass

    # Copy template into a task-owned file so the session work_dir can be
    # cleaned up independently of the background task lifecycle.
    task_id = _new_task()
    task_tpl = os.path.join(UPLOAD_DIR, f"task_{task_id}.docx")
    shutil.copy(tpl_path, task_tpl)

    def worker():
        try:
            generate_notices(
                task_tpl, data_rows, manual_fields,
                filename_fields=filename_fields,
                group_by_field=group_by_field,
                output_format=output_format,
                task_id=task_id,
                render_workers=profile["render_workers"],
                convert_workers=profile["convert_workers"],
                pdf_chunk_size=profile["pdf_chunk_size"],
                profile_label=profile["label"],
            )
        except Exception as e:
            traceback.print_exc()
            _update_task(task_id, status="error", stage="error",
                         error=f"{e.__class__.__name__}: {e}",
                         message=f"Generation failed: {e}")
        finally:
            try:
                os.remove(task_tpl)
            except OSError:
                pass
            # Session files served their purpose; clean them up.
            shutil.rmtree(os.path.join(UPLOAD_DIR, sid), ignore_errors=True)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify(task_id=task_id)


@app.route("/status/<task_id>")
def status(task_id):
    task = _get_task(task_id)
    if task is None:
        return jsonify(error="Unknown task."), 404
    # Strip disk paths from the public response.
    parts_public = [
        {"index": p["index"], "name": p["name"], "group": p.get("group", "")}
        for p in (task.get("ready_parts") or [])
    ]
    return jsonify(
        status=task["status"],
        stage=task["stage"],
        progress=task["progress"],
        total=task["total"],
        message=task["message"],
        error=task["error"],
        groups_total=task.get("groups_total", 0),
        groups_done=task.get("groups_done", 0),
        current_group=task.get("current_group", ""),
        group_total=task.get("group_total", 0),
        group_progress=task.get("group_progress", 0),
        ready_parts=parts_public,
    )


@app.route("/download/<task_id>/<int:part_index>")
def download_part(task_id, part_index):
    task = _get_task(task_id)
    if task is None:
        return jsonify(error="Unknown task."), 404
    parts = task.get("ready_parts") or []
    if part_index < 0 or part_index >= len(parts):
        return jsonify(error="Part not ready."), 404

    part = parts[part_index]
    zip_path = part.get("path")
    if not zip_path or not os.path.exists(zip_path):
        return jsonify(error="Part file missing."), 404

    # Clean up this part's zip after a generous delay so retries work.
    def delayed_cleanup():
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except OSError:
            pass
    threading.Timer(600.0, delayed_cleanup).start()

    return send_file(zip_path, mimetype="application/zip",
                     as_attachment=True, download_name=part["name"])


# ── HTML template ────────────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>S&amp;S Law Firm — Legal Notice Generator</title>
<link rel="icon" type="image/png" href="__LOGO_DATA_URI__">
<style>
  :root { --primary: #2563eb; --danger: #dc2626; --success: #16a34a;
          --bg: #f8fafc; --card: #fff; --brand: #0b1220; --brand-accent: #c9a14a; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", Roboto, "PingFang SC", sans-serif;
         background: var(--bg); color: #1e293b; line-height: 1.6; }

  .brand-bar { background: linear-gradient(180deg, #0b1220 0%, #111a2e 100%);
               border-bottom: 3px solid var(--brand-accent);
               box-shadow: 0 2px 12px rgba(0,0,0,.18);
               position: relative; }
  .brand-inner { max-width: 860px; margin: 0 auto; padding: 28px 16px 24px;
                 display: flex; flex-direction: column; align-items: center; gap: 10px; }
  .brand-logout { position: absolute; top: 14px; right: 18px;
                  color: #cbd5e1; text-decoration: none; font-size: .72rem;
                  letter-spacing: .1em; text-transform: uppercase;
                  padding: 6px 12px; border: 1px solid rgba(201,161,74,.35);
                  border-radius: 4px; transition: .2s; }
  .brand-logout:hover { color: #c9a14a; border-color: #c9a14a;
                        background: rgba(201,161,74,.08); }
  .brand-logo { max-width: 340px; width: 82%; height: auto; display: block;
                filter: drop-shadow(0 2px 8px rgba(0,0,0,.35)); }
  .brand-tagline { color: #cbd5e1; font-size: .82rem; letter-spacing: .18em;
                   text-transform: uppercase; font-weight: 500;
                   border-top: 1px solid rgba(201,161,74,.35); padding-top: 10px;
                   margin-top: 4px; }
  .brand-tagline span { color: var(--brand-accent); }

  .container { max-width: 860px; margin: 0 auto; padding: 32px 16px; }
  h1 { text-align: center; font-size: 1.5rem; margin-bottom: 6px; color: #0b1220;
       letter-spacing: .01em; }
  .subtitle { text-align: center; color: #64748b; margin-bottom: 32px; font-size: .92rem; }

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
  .field-group input[type="text"], .field-group select {
    width: 100%; padding: 8px 12px; border: 1px solid #cbd5e1; border-radius: 6px;
    font-size: .95rem; }
  .field-group input[type="text"]:focus, .field-group select:focus {
    outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(37,99,235,.15); }
  .field-group .hint { font-size: .78rem; color: #94a3b8; margin-top: 4px; }

  .check-grid { display: flex; flex-wrap: wrap; gap: 6px 14px; padding: 8px 12px;
                border: 1px solid #cbd5e1; border-radius: 6px; max-height: 140px;
                overflow-y: auto; background: #fff; }
  .check-grid label { font-size: .88rem; font-weight: 400; display: inline-flex;
                      align-items: center; gap: 4px; cursor: pointer; margin: 0; }
  .check-grid input[type="checkbox"] { margin: 0; }

  .info-box { padding: 12px 16px; border-radius: 8px; font-size: .9rem; margin-bottom: 16px; }
  .info-ok { background: #f0fdf4; border: 1px solid #bbf7d0; color: #166534; }
  .info-warn { background: #fffbeb; border: 1px solid #fde68a; color: #92400e; }

  table.preview { width: 100%; border-collapse: collapse; font-size: .85rem; margin-top: 8px; }
  table.preview th, table.preview td { padding: 6px 10px; border: 1px solid #e2e8f0; text-align: left; }
  table.preview th { background: #f1f5f9; font-weight: 600; }

  .progress-wrap { margin-top: 16px; }
  .progress-bar { width: 100%; height: 10px; background: #e2e8f0; border-radius: 999px; overflow: hidden; }
  .progress-fill { height: 100%; background: var(--primary); width: 0%; transition: width .3s ease; }
  .progress-text { font-size: .85rem; color: #64748b; margin-top: 6px; text-align: center; }

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
<header class="brand-bar">
  <a class="brand-logout" href="/logout">Sign Out</a>
  <div class="brand-inner">
    <img class="brand-logo" src="__LOGO_DATA_URI__" alt="S&amp;S Law Firm">
    <div class="brand-tagline">Batch <span>Legal Notice</span> Generator</div>
  </div>
</header>
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

    <div class="field-group" id="dateSection" style="display:none">
      <label>Notice Date &mdash; used for &#123;&#123;date&#125;&#125; (always overrides any Excel column)</label>
      <input type="date" id="dateValue">
      <div class="hint">Output format: DD/MM/YYYY. Any <code>date</code> column in your Excel is ignored.</div>
    </div>

    <div class="field-group">
      <label>File Naming &mdash; pick one or more Excel columns (joined by _)</label>
      <div class="check-grid" id="filenameFields"></div>
      <div class="hint">If none selected, files are numbered sequentially.</div>
    </div>

    <div class="row">
      <div class="field-group">
        <label>Group By</label>
        <select id="groupByField">
          <option value="">No grouping (single output.zip)</option>
        </select>
        <div class="hint">When set, rows are sorted by this column and each group is downloaded as its own zip as soon as it's ready.</div>
      </div>
      <div class="field-group">
        <label>Output Format</label>
        <select id="outputFormat">
          <option value="docx" selected>DOCX (default)</option>
          <option value="pdf">PDF</option>
        </select>
      </div>
    </div>

    <div class="field-group">
      <label>Machine Profile</label>
      <select id="machineProfile">
        <option value="vps" selected>VPS &mdash; conservative (default)</option>
        <option value="mac">Mac &mdash; max performance (all cores)</option>
      </select>
      <div class="hint">Pick &ldquo;Mac&rdquo; only when running locally on a powerful multi-core machine. It enables process-pool rendering and uses every core &mdash; much faster for DOCX output, but heavier on the box.</div>
    </div>

    <br>
    <button class="btn btn-primary btn-block" id="genBtn" onclick="doGenerate()">
      Generate Notices
    </button>

    <div class="progress-wrap" id="progressSection" style="display:none">
      <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
      <div class="progress-text" id="progressText">Starting...</div>
    </div>
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
  tagsDiv.innerHTML = data.placeholders.map(function(p) {
    const ok = data.matched.indexOf(p) !== -1;
    return '<span class="tag ' + (ok ? 'tag-ok' : 'tag-miss') + '">&#123;&#123;' + p + '&#125;&#125;</span>';
  }).join('');

  // "date" is handled by the webpage date picker — hide it from the missing
  // list and do not render a manual text input for it.
  const manualMissing = data.missing.filter(function(p) { return p !== 'date'; });
  if (manualMissing.length > 0) {
    document.getElementById('manualSection').style.display = '';
    document.getElementById('manualFields').innerHTML = manualMissing.map(function(p) {
      return '<div class="field-group"><label>&#123;&#123;' + p + '&#125;&#125;</label>' +
        '<input type="text" data-field="' + p + '" placeholder="Enter value (shared across all notices)"></div>';
    }).join('');
  } else {
    document.getElementById('manualSection').style.display = 'none';
  }

  // Show the date picker whenever the template has a {{date}} placeholder.
  // Default it to today; the user can change it.
  const dateSection = document.getElementById('dateSection');
  const dateInput = document.getElementById('dateValue');
  if (data.placeholders.indexOf('date') !== -1) {
    dateSection.style.display = '';
    if (!dateInput.value) {
      const t = new Date();
      const y = t.getFullYear();
      const m = String(t.getMonth() + 1).padStart(2, '0');
      const d = String(t.getDate()).padStart(2, '0');
      dateInput.value = y + '-' + m + '-' + d;
    }
  } else {
    dateSection.style.display = 'none';
    dateInput.value = '';
  }

  if (data.preview.length > 0) {
    const headers = data.excel_headers;
    let html = '<table class="preview"><thead><tr><th>#</th>';
    headers.forEach(function(h) { html += '<th>' + h + '</th>'; });
    html += '</tr></thead><tbody>';
    data.preview.forEach(function(row, i) {
      html += '<tr><td>' + (i + 1) + '</td>';
      headers.forEach(function(h) { html += '<td>' + (row[h] !== undefined && row[h] !== null ? row[h] : '') + '</td>'; });
      html += '</tr>';
    });
    html += '</tbody></table>';
    document.getElementById('previewTable').innerHTML = html;
  }

  // Populate filename checkbox grid — nothing is pre-selected, user picks
  // the columns themselves in the webpage.
  const fnBox = document.getElementById('filenameFields');
  fnBox.innerHTML = data.excel_headers.map(function(h) {
    return '<label><input type="checkbox" value="' + h + '">' + h + '</label>';
  }).join('');

  // Populate group-by dropdown — no pre-selection.
  const gb = document.getElementById('groupByField');
  gb.innerHTML = '<option value="">No grouping (single output.zip)</option>';
  data.excel_headers.forEach(function(h) {
    gb.innerHTML += '<option value="' + h + '">' + h + '</option>';
  });
}

async function doGenerate() {
  const btn = document.getElementById('genBtn');
  const status = document.getElementById('genStatus');
  const progressSection = document.getElementById('progressSection');
  const progressFill = document.getElementById('progressFill');
  const progressText = document.getElementById('progressText');

  btn.disabled = true;
  const fmt = document.getElementById('outputFormat').value;
  btn.innerHTML = '<span class="spinner"></span>Processing...';

  const manualFields = {};
  document.querySelectorAll('#manualFields input[data-field]').forEach(function(input) {
    manualFields[input.dataset.field] = input.value;
  });

  const filenameFields = Array.prototype.slice.call(
    document.querySelectorAll('#filenameFields input[type="checkbox"]:checked')
  ).map(function(cb) { return cb.value; });
  const groupByField = document.getElementById('groupByField').value;

  progressSection.style.display = '';
  progressFill.style.width = '0%';
  progressText.textContent = 'Starting...';
  status.innerHTML = '';
  status.style.color = '#64748b';

  // Track which ready_parts we've already triggered downloads for, so
  // we don't re-download them on each subsequent poll.
  const triggered = new Set();
  const downloadedNames = [];

  function triggerDownload(tid, part) {
    const a = document.createElement('a');
    a.href = '/download/' + tid + '/' + part.index;
    a.download = part.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    downloadedNames.push(part.name);
    renderDownloadedList();
  }

  function renderDownloadedList() {
    if (downloadedNames.length === 0) { status.innerHTML = ''; return; }
    status.innerHTML = 'Downloaded (' + downloadedNames.length + '): <b>' +
      downloadedNames.join('</b>, <b>') + '</b>';
    status.style.color = 'var(--success)';
  }

  let taskId = null;
  try {
    const resp = await fetch('/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manual_fields: manualFields,
        filename_fields: filenameFields,
        group_by_field: groupByField,
        output_format: fmt,
        date_value: document.getElementById('dateValue').value,
        machine: document.getElementById('machineProfile').value,
      })
    });
    const data = await resp.json();
    if (data.error) { alert(data.error); return; }
    taskId = data.task_id;

    // Poll status until done. On each poll:
    //   1. update overall + per-group progress
    //   2. trigger a download for any newly-ready group part
    while (true) {
      await new Promise(function(r) { setTimeout(r, 800); });
      const sr = await fetch('/status/' + taskId);
      const s = await sr.json();
      if (s.error) throw new Error(s.error);

      const pct = s.total > 0 ? Math.round((s.progress / s.total) * 100) : 0;
      progressFill.style.width = pct + '%';

      // Build a detailed progress line so the user sees both the overall
      // picture and what's happening in the current group right now.
      let line = s.stage + ' — overall ' + s.progress + '/' + s.total + ' (' + pct + '%)';
      if (s.groups_total) {
        const gIdx = s.status === 'done' ? s.groups_total
                    : Math.min(s.groups_done + 1, s.groups_total);
        line += ' · group ' + gIdx + '/' + s.groups_total;
        if (s.current_group && s.status !== 'done') {
          line += ' [' + s.current_group;
          if (s.group_total) {
            line += ' ' + s.group_progress + '/' + s.group_total;
          }
          line += ']';
        }
      }
      progressText.textContent = line;

      // Trigger downloads for any newly-ready parts.
      (s.ready_parts || []).forEach(function(part) {
        if (!triggered.has(part.index)) {
          triggered.add(part.index);
          triggerDownload(taskId, part);
        }
      });

      if (s.status === 'done') break;
      if (s.status === 'error') throw new Error(s.error || 'Generation failed');
    }

    progressText.textContent = 'All ' + downloadedNames.length + ' group(s) processed and downloaded.';
  } catch (e) {
    status.textContent = 'Generation failed: ' + e.message;
    status.style.color = 'var(--danger)';
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

LOGIN_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>S&amp;S Law Firm — Sign In</title>
<link rel="icon" type="image/png" href="__LOGO_DATA_URI__">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", Roboto, "PingFang SC", sans-serif;
         min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: radial-gradient(ellipse at top, #1a2541 0%, #0b1220 70%);
         color: #e2e8f0; padding: 20px; }
  .login-card { background: rgba(17, 26, 46, 0.92);
                border: 1px solid rgba(201,161,74,.28);
                border-top: 3px solid #c9a14a;
                border-radius: 12px;
                padding: 38px 34px 34px;
                width: 100%; max-width: 400px;
                box-shadow: 0 24px 60px rgba(0,0,0,.55); }
  .login-logo { display: block; max-width: 260px; width: 86%; height: auto;
                margin: 0 auto 14px;
                filter: drop-shadow(0 2px 10px rgba(0,0,0,.5)); }
  .login-tagline { text-align: center; color: #cbd5e1; font-size: .78rem;
                   letter-spacing: .18em; text-transform: uppercase;
                   font-weight: 500;
                   border-top: 1px solid rgba(201,161,74,.32);
                   padding-top: 12px; margin-bottom: 26px; }
  .login-tagline span { color: #c9a14a; }
  .field { margin-bottom: 20px; }
  .field label { display: block; font-size: .74rem; color: #94a3b8;
                 letter-spacing: .14em; text-transform: uppercase;
                 margin-bottom: 8px; font-weight: 600; }
  .field input { width: 100%; padding: 12px 14px; border-radius: 6px;
                 background: #0b1220; color: #e2e8f0; font-size: 1rem;
                 border: 1px solid rgba(255,255,255,.09); outline: none;
                 transition: border-color .2s, box-shadow .2s; }
  .field input:focus { border-color: #c9a14a;
                       box-shadow: 0 0 0 3px rgba(201,161,74,.18); }
  button { width: 100%; padding: 13px; border: none; border-radius: 6px;
           background: #c9a14a; color: #0b1220; font-weight: 700;
           font-size: .92rem; cursor: pointer;
           letter-spacing: .08em; text-transform: uppercase;
           transition: background .2s, transform .05s; }
  button:hover { background: #d4af5f; }
  button:active { transform: translateY(1px); }
  .error { background: rgba(220,38,38,.14);
           border: 1px solid rgba(220,38,38,.4);
           color: #fca5a5; padding: 10px 14px; border-radius: 6px;
           font-size: .86rem; margin-bottom: 18px; text-align: center; }
</style>
</head>
<body>
<form class="login-card" method="POST" action="/login">
  <img class="login-logo" src="__LOGO_DATA_URI__" alt="S&amp;S Law Firm">
  <div class="login-tagline">Batch <span>Legal Notice</span> Generator</div>
  __ERROR__
  <div class="field">
    <label for="password">Access Password</label>
    <input type="password" id="password" name="password" autofocus required>
  </div>
  <button type="submit">Sign In</button>
</form>
</body>
</html>
"""

# Inlined S&S Law Firm logo (base64 PNG) — single-file delivery constraint.
LOGO_DATA_URI = "data:image/png;base64,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"
HTML_TEMPLATE = HTML_TEMPLATE.replace("__LOGO_DATA_URI__", LOGO_DATA_URI)
LOGIN_TEMPLATE = LOGIN_TEMPLATE.replace("__LOGO_DATA_URI__", LOGO_DATA_URI)


if __name__ == "__main__":
    print("=" * 50)
    print("  Legal Notice Generator")
    print(f"  Detected CPUs: {_CPU}")
    print(f"  Machine profiles (pick at generate time in the webpage):")
    for _name, _prof in MACHINE_PROFILES.items():
        _marker = " (default)" if _name == DEFAULT_MACHINE else ""
        print(f"    - {_name:4s}{_marker}  render={_prof['render_workers']}  "
              f"convert={_prof['convert_workers']}  "
              f"chunk={_prof['pdf_chunk_size']}  [{_prof['label']}]")
    print("  Open browser: http://127.0.0.1:5002")
    print("=" * 50)
    # threaded=True is required so the /status and /download polling hits
    # the same process as the background worker thread.
    app.run(debug=False, port=5002, threaded=True)
