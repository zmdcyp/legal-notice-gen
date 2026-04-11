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
                   Response, after_this_request)
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
