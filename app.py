"""PDF Processing FastAPI Application with granular lifecycle logging and visual dashboard."""

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from pypdf import PdfReader
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from logger import RequestLogger, configure_logging
from camelot_table_extractor import DEFAULT_TABLES_DIR, extract_and_store_tables

configure_logging()

app = FastAPI(title="PDF Processing API", version="1.0.0", docs_url=None)


def openapi_with_fresh_id():
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    for ex in schema.get("components", {}).get("schemas", {}).get("ProcessPdfRequest", {}).get("examples", []):
        ex["request_id"] = str(uuid4())
    return schema


app.openapi = openapi_with_fresh_id


@app.get("/docs", include_in_schema=False)
def custom_docs():
    res = get_swagger_ui_html(openapi_url=app.openapi_url, title=app.title + " - Swagger UI")
    inject = """<script>
document.addEventListener('click', (e) => {
  if (!e.target.closest('.try-it-out, .try-it-out-btn')) return;
  let n = 0;
  const tick = () => {
    let stamped = false;
    document.querySelectorAll('textarea.body-param__text').forEach((t) => {
      try {
        const body = JSON.parse(t.value);
        if ('request_id' in body) { body.request_id = crypto.randomUUID(); t.value = JSON.stringify(body, null, 2); stamped = true; }
      } catch {}
    });
    if (!stamped && ++n < 20) setTimeout(tick, 50);
  };
  setTimeout(tick, 50);
});
</script>"""
    return HTMLResponse(res.body.decode("utf-8").replace("</body>", inject + "</body>"))


_SEEN_REQUEST_IDS: set[str] = set()
_SEEN_LOCK = threading.Lock()
_LONE_BACKSLASH_RE = re.compile(rb'\\(?!\\)')


def repair_unescaped_backslashes(raw_body: bytes) -> Optional[bytes]:
    try:
        json.loads(raw_body)
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    repaired = _LONE_BACKSLASH_RE.sub(rb'\\\\', raw_body)
    try:
        json.loads(repaired)
        return repaired
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


class JsonPathRepairMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") == "POST" and scope.get("path") == "/process-pdf":
            buffered: list[Message] = []
            body = b""
            while True:
                msg = await receive()
                buffered.append(msg)
                body += msg.get("body", b"")
                if not msg.get("more_body"):
                    break
            repaired = repair_unescaped_backslashes(body)
            if repaired is not None:
                req_id = "INVALID_REQUEST"
                try:
                    req_id = str(json.loads(repaired).get("request_id", req_id))
                except Exception:
                    pass
                RequestLogger(req_id).info("JSON body contained unescaped backslashes (Windows path); repaired automatically.")
                buffered = [{"type": "http.request", "body": repaired, "more_body": False}]

            async def replay_receive() -> Message:
                return buffered.pop(0) if buffered else {"type": "http.disconnect"}

            await self.app(scope, replay_receive, send)
            return
        await self.app(scope, receive, send)


app.add_middleware(JsonPathRepairMiddleware)


class ProcessPdfRequest(BaseModel):
    request_id: str = Field(..., description="Unique non-sequential request identifier")
    pdf_path: str = Field(..., description="Path to PDF file")

    model_config = {"json_schema_extra": {"examples": [{"request_id": "", "pdf_path": "sample.pdf"}]}}

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("request_id cannot be empty or whitespace.")
        if s.isdigit():
            raise ValueError(f"Invalid request_id '{v}': must be a unique identifier, not simple sequential numbers like 1, 2, 3...")
        return s

    @field_validator("pdf_path")
    @classmethod
    def validate_pdf_path(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("pdf_path cannot be empty or whitespace.")
        return s


def extract_pdf_metadata(reader: PdfReader) -> Dict[str, Any]:
    raw = reader.metadata or {}
    key_map = {
        "title": "/Title",
        "author": "/Author",
        "subject": "/Subject",
        "creator": "/Creator",
        "producer": "/Producer",
        "creation_date": "/CreationDate",
        "modification_date": "/ModDate",
    }
    meta = {}
    for field, pdf_key in key_map.items():
        val = getattr(raw, field, None)
        if val is None:
            val = raw.get(pdf_key)
        meta[field] = val.isoformat() if hasattr(val, "isoformat") else (str(val) if val is not None else None)
    return meta


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    req_id = "INVALID_REQUEST"
    try:
        body = await request.body()
        if body:
            req_id = str(json.loads(body.decode("utf-8")).get("request_id", req_id))
    except Exception:
        pass
    logger = RequestLogger(req_id)
    msg = "; ".join(f"{' -> '.join(str(x) for x in e.get('loc', []))}: {e.get('msg', '')}" for e in exc.errors())
    logger.info(f"Request received with invalid payload: {msg}")
    logger.error(f"RequestValidationError: {msg}")
    logger.info("Response sent: 422 Unprocessable Entity")
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors()), "message": msg})


def _check_request_id(req_id: str, logger: RequestLogger) -> None:
    if req_id.isdigit():
        err = f"Invalid request_id '{req_id}': must be a unique identifier, not simple sequential numbers like 1, 2, 3..."
        logger.error(err)
        logger.info("Response sent: 422 Unprocessable Entity")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err)
    with _SEEN_LOCK:
        if req_id in _SEEN_REQUEST_IDS:
            err = f"ConflictError: Duplicate request_id: '{req_id}' has already been processed. Each request must have a unique request_id."
            logger.error(err)
            logger.info("Response sent: 409 Conflict")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=err)
        _SEEN_REQUEST_IDS.add(req_id)


def _process_pdf_file(file_path: Path, req_id: str, logger: RequestLogger) -> Dict[str, Any]:
    logger.info(f"Opening PDF file '{file_path.name}'")
    try:
        reader = PdfReader(str(file_path.resolve()))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass
        logger.info("PDF opened successfully")
    except Exception as e:
        err = f"Failed to open PDF file '{file_path}': {type(e).__name__}: {str(e)}"
        logger.error(err, exc_info=True)
        logger.info("Response sent: 422 Unprocessable Entity")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err)

    logger.info("Task started: extracting page count and metadata")
    try:
        page_count = len(reader.pages)
        logger.info(f"Page count extracted: {page_count} page(s)")
        metadata = extract_pdf_metadata(reader)
        fields = [k for k, v in metadata.items() if v is not None]
        logger.info(f"Basic metadata extracted: {len(fields)} field(s) populated ({', '.join(fields)})")

        try:
            tables_summary = extract_and_store_tables(str(file_path.resolve()), req_id)
            logger.info(f"Table extraction: {tables_summary['table_count']} lined table(s), {tables_summary['row_count']} row(s) saved across {len(tables_summary.get('json_files', []))} individual JSON file(s)")
        except Exception as table_err:
            logger.error(f"Failed to extract tables: {type(table_err).__name__}: {str(table_err)}", exc_info=True)
            tables_summary = {"json_files": [], "table_count": 0, "row_count": 0, "tables": []}

        logger.info(f"Task completed: Successfully processed '{file_path.name}' ({page_count} page(s))")
    except Exception as e:
        err = f"Failed to extract PDF data: {type(e).__name__}: {str(e)}"
        logger.error(err, exc_info=True)
        logger.info("Response sent: 500 Internal Server Error")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=err)

    return {
        "pdf_path": str(file_path.resolve()),
        "page_count": page_count,
        "metadata": metadata,
        "tables": tables_summary,
    }


HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>PDF Processing API - Visual Dashboard</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0b0f19; color: #e2e8f0; margin: 0; padding: 24px; }
    .box { max-width: 900px; margin: 0 auto; background: #151d30; border-radius: 12px; padding: 28px; border: 1px solid #243049; }
    .top-bar { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #243049; padding-bottom: 16px; margin-bottom: 20px; }
    h1 { margin: 0; font-size: 20px; color: #f8fafc; }
    a { color: #38bdf8; text-decoration: none; font-size: 13px; font-weight: 600; }
    .form-group { margin-bottom: 14px; }
    label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; color: #94a3b8; }
    .input-row { display: flex; gap: 8px; }
    input[type="text"], input[type="file"] { width: 100%; padding: 9px 12px; background: #0b0f19; border: 1px solid #334155; border-radius: 6px; color: #fff; font-size: 14px; }
    .preset-row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
    .btn-preset { background: #1e293b; color: #94a3b8; border: 1px solid #334155; padding: 4px 10px; border-radius: 4px; font-size: 12px; cursor: pointer; }
    .btn-preset:hover { background: #334155; color: #fff; }
    .btn-main { width: 100%; margin-top: 14px; padding: 12px; background: #2563eb; color: #fff; border: 0; border-radius: 6px; cursor: pointer; font-weight: 700; font-size: 14px; }
    .btn-main:hover { background: #1d4ed8; }
    #visualResults { display: none; margin-top: 24px; }
    .stat-hero { display: flex; gap: 20px; align-items: center; background: linear-gradient(135deg, #1e293b, #0f172a); border: 1px solid #38bdf844; border-radius: 10px; padding: 20px; margin-bottom: 16px; }
    .page-stat { text-align: center; min-width: 100px; padding-right: 20px; border-right: 1px solid #334155; }
    .stat-num { font-size: 40px; font-weight: 800; color: #38bdf8; line-height: 1; }
    .meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin-bottom: 16px; }
    .meta-card { background: #0e1626; border: 1px solid #243049; border-radius: 8px; padding: 12px 16px; }
    .m-label { font-size: 11px; font-weight: 700; color: #64748b; text-transform: uppercase; margin-bottom: 4px; }
    .m-val { font-size: 14px; color: #f1f5f9; word-break: break-word; }
    pre { background: #080c14; padding: 14px; border-radius: 8px; overflow-x: auto; font-size: 12px; max-height: 220px; color: #cbd5e1; }
  </style>
</head>
<body>
  <div class="box">
    <div class="top-bar">
      <h1>📄 PDF Processing API</h1>
      <a href="/docs" target="_blank">Swagger OpenAPI Docs ↗</a>
    </div>
    <form id="f" onsubmit="run(event)">
      <div class="form-group">
        <label>Request ID</label>
        <div class="input-row">
          <input id="rid" required autocomplete="off" />
          <button type="button" class="btn-preset" onclick="genId()" style="padding:0 12px;">Regenerate</button>
        </div>
      </div>
      <div class="form-group">
        <label>Choose Preloaded Sample PDF Path</label>
        <input id="p" value="sample_pdf_for_processing_api.pdf" />
        <div class="preset-row">
          <button type="button" class="btn-preset" onclick="setPreset('sample_pdf_for_processing_api.pdf')">Sample 1</button>
          <button type="button" class="btn-preset" onclick="setPreset('sample_pdf_for_stream_extraction.pdf')">Sample 2</button>
          <button type="button" class="btn-preset" onclick="setPreset('sample.pdf')">Sample 3</button>
        </div>
      </div>
      <div class="form-group">
        <label>…Or Upload PDF From Your Device</label>
        <input id="fileInput" type="file" accept=".pdf,application/pdf" />
      </div>
      <button id="b" class="btn-main">Process PDF Document</button>
    </form>
    <div id="visualResults">
      <div class="stat-hero">
        <div class="page-stat"><div class="stat-num" id="visPages">0</div><div style="font-size:11px;color:#94a3b8;">PAGES</div></div>
        <div><h2 id="visTitle" style="margin:0 0 6px;font-size:18px;"></h2><div id="visPath" style="font-size:12px;color:#64748b;font-family:monospace;"></div></div>
      </div>
      <div class="meta-grid">
        <div class="meta-card"><div class="m-label">Author</div><div class="m-val" id="visAuthor">-</div></div>
        <div class="meta-card"><div class="m-label">Subject</div><div class="m-val" id="visSubject">-</div></div>
        <div class="meta-card"><div class="m-label">Creator</div><div class="m-val" id="visCreator">-</div></div>
        <div class="meta-card"><div class="m-label">Producer</div><div class="m-val" id="visProducer">-</div></div>
      </div>

      <!-- Render Extracted Table with Proper Grid Lines -->
      <div id="gridTableContainer" style="display: none; margin-top: 20px;">
        <h3 style="margin: 0 0 10px 0; font-size: 15px; color: #38bdf8;">📊 Extracted Table Data (with Grid Lines)</h3>
        <div id="gridTablesList"></div>
      </div>

      <details style="margin-top: 16px;"><summary style="cursor:pointer;color:#94a3b8;font-size:12px;font-weight:600;">View Raw JSON Response</summary><pre id="rawJson"></pre></details>
    </div>
  </div>
  <script>
    function setPreset(v) { document.getElementById('p').value = v; }
    function genId() { document.getElementById('rid').value = crypto.randomUUID(); }
    genId();
    function render(id, val) { document.getElementById(id).textContent = val || '—'; }
    async function run(e) {
      e.preventDefault();
      const b = document.getElementById('b');
      b.disabled = true; b.textContent = '⏳ Processing PDF...';
      const rid = document.getElementById('rid').value.trim();
      try {
        const fi = document.getElementById('fileInput');
        let res;
        if (fi.files.length > 0) {
          const fd = new FormData();
          fd.append('request_id', rid);
          fd.append('file', fi.files[0]);
          res = await fetch('/upload-pdf', { method: 'POST', body: fd });
        } else {
          res = await fetch('/process-pdf', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ request_id: rid, pdf_path: document.getElementById('p').value.trim() })
          });
        }
        const data = await res.json();
        if (res.ok && data.data) {
          document.getElementById('visualResults').style.display = 'block';
          document.getElementById('visPages').textContent = data.data.page_count;
          const meta = data.data.metadata || {};
          document.getElementById('visTitle').textContent = meta.title || data.data.pdf_path.split(/[\\\\/]/).pop();
          document.getElementById('visPath').textContent = data.data.pdf_path;
          render('visAuthor', meta.author);
          render('visSubject', meta.subject);
          render('visCreator', meta.creator);
          render('visProducer', meta.producer);
          document.getElementById('rawJson').textContent = JSON.stringify(data, null, 2);

          // Render Grid Tables
          const tablesData = (data.data.tables && data.data.tables.tables) || [];
          const gridContainer = document.getElementById('gridTableContainer');
          const gridList = document.getElementById('gridTablesList');

          const validTables = tablesData.filter(t => t.rows && t.rows.length > 0);
          if (validTables.length > 0) {
            gridList.innerHTML = validTables.map((table, tIdx) => {
              const headers = Object.keys(table.rows[0]);
              const ths = headers.map(h => `<th style="padding: 10px 14px; border: 1px solid #334155; font-weight: 700; color: #38bdf8; background: #1e293b;">${h}</th>`).join('');
              const trs = table.rows.map((row, rIdx) => {
                const bg = rIdx % 2 === 0 ? '#0f172a' : '#141e33';
                return `<tr style="background: ${bg};">` + headers.map(h => `<td style="padding: 9px 14px; border: 1px solid #334155;">${row[h] || ''}</td>`).join('') + '</tr>';
              }).join('');
              const title = validTables.length > 1 ? `<div style="font-size: 13px; font-weight: 600; color: #94a3b8; margin: 12px 0 6px 0;">Table ${tIdx + 1} (Page ${table.page || 1})</div>` : '';
              return `${title}<div style="overflow-x: auto; background: #0e1626; border-radius: 8px; border: 1px solid #334155; padding: 12px; margin-bottom: 12px;"><table style="width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; color: #f8fafc;"><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table></div>`;
            }).join('');
            gridContainer.style.display = 'block';
          } else {
            gridContainer.style.display = 'none';
          }
        } else {
          alert('Error: ' + (data.detail || JSON.stringify(data)));
        }
      } catch (err) {
        alert('Request failed: ' + err.message);
      } finally {
        b.disabled = false; b.textContent = 'Process PDF Document';
        genId();
      }
    }
  </script>
</body>
</html>
"""


@app.get("/")
def root(request: Request):
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(content=HTML_DASHBOARD)
    return {
        "service": "PDF Processing API",
        "status": "online",
        "endpoints": {"POST /process-pdf": "Process a PDF", "POST /upload-pdf": "Upload and process a PDF"},
    }


@app.get("/extracted-tables")
def extracted_tables() -> Dict[str, Any]:
    logger = RequestLogger("TABLES_VIEW")
    files = [str(f.resolve()) for f in sorted(DEFAULT_TABLES_DIR.glob("*.json"), reverse=True)] if DEFAULT_TABLES_DIR.exists() else []
    logger.info(f"Response sent: 200 OK ({len(files)} table file(s))")
    return {"status": "success", "count": len(files), "json_files": files}


@app.post("/upload-pdf")
async def upload_pdf(request_id: str = Form(...), file: UploadFile = File(...)) -> Dict[str, Any]:
    req_id = request_id.strip()
    logger = RequestLogger(req_id)
    logger.info(f"Request received (device upload): request_id='{req_id}', filename='{file.filename}'")
    _check_request_id(req_id, logger)

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        err = f"ValueError: Only .pdf files can be uploaded. Got: '{file.filename}'"
        logger.error(err)
        logger.info("Response sent: 400 Bad Request")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)

    upload_dir = Path("uploads")
    upload_dir.mkdir(exist_ok=True)
    upload_path = upload_dir / f"{req_id}_{Path(file.filename).name}"
    with upload_path.open("wb") as f:
        f.write(await file.read())
    logger.info(f"Uploaded file saved to '{upload_path}'")

    data = _process_pdf_file(upload_path, req_id, logger)
    logger.info("Response sent: 200 OK")
    return {"request_id": req_id, "status": "success", "data": data}


@app.post("/process-pdf")
async def process_pdf(payload: ProcessPdfRequest) -> Dict[str, Any]:
    req_id, pdf_path = payload.request_id, payload.pdf_path
    logger = RequestLogger(req_id)
    logger.info(f"Request received: request_id='{req_id}', pdf_path='{pdf_path}'")
    _check_request_id(req_id, logger)

    logger.info(f"File path validation started for '{pdf_path}'")
    fp = Path(pdf_path)
    if not fp.exists():
        err = f"FileNotFoundError: The PDF file does not exist at '{pdf_path}'"
        logger.error(err)
        logger.info("Response sent: 404 Not Found")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err)
    if not fp.is_file():
        err = f"IsADirectoryError: Path '{pdf_path}' is a directory, not a file."
        logger.error(err)
        logger.info("Response sent: 400 Bad Request")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)
    if fp.suffix.lower() != ".pdf":
        err = f"ValueError: Invalid file extension '{fp.suffix}'. Expected a '.pdf' file."
        logger.error(err)
        logger.info("Response sent: 400 Bad Request")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)
    logger.info(f"File path validation passed: file exists at '{fp.resolve()}'")

    data = _process_pdf_file(fp, req_id, logger)
    logger.info("Response sent: 200 OK")
    return {"request_id": req_id, "status": "success", "data": data}
