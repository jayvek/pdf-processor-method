"""PDF Processing FastAPI Application with granular lifecycle logging and visual dashboard."""

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4  # [ADDED] fresh request_id per /openapi.json fetch

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status  # [UPDATED] added File, Form, UploadFile for device PDF upload
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html  # [ADDED] custom Swagger UI with dynamic request_id
from fastapi.openapi.utils import get_openapi  # [ADDED] regenerate schema with fresh request_id
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from pypdf import PdfReader
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from logger import RequestLogger, configure_logging

configure_logging()

app = FastAPI(title="PDF Processing API", version="1.0.0", docs_url=None)  # [UPDATED] built-in /docs disabled, custom one below


# --- Swagger UI with a fresh request_id on every refresh / "Try it out" -----
def openapi_with_fresh_id():  # [ADDED] regenerate schema each fetch → new uuid in example
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    for ex in schema.get("components", {}).get("schemas", {}).get("ProcessPdfRequest", {}).get("examples", []):
        ex["request_id"] = str(uuid4())
    return schema


app.openapi = openapi_with_fresh_id  # [ADDED] bypass FastAPI's schema cache


@app.get("/docs", include_in_schema=False)  # [ADDED]
def custom_docs():
    response = get_swagger_ui_html(openapi_url=app.openapi_url, title=app.title + " - Swagger UI")
    inject = """
<script>
document.addEventListener('click', (e) => {
  if (!e.target.closest('.try-it-out, .try-it-out-btn')) return;
  let n = 0; // [ADDED] retry until Swagger renders the body textarea (max ~1s)
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
    return HTMLResponse(response.body.decode("utf-8").replace("</body>", inject + "</body>"))

_SEEN_REQUEST_IDS: set[str] = set()
_SEEN_LOCK = threading.Lock()


# --- Tolerant JSON handling for Windows paths -------------------------------
# Some clients send JSON bodies containing Windows paths with literal
# backslashes ("C:\Users\..."). Backslash sequences like \U or \A are not
# valid JSON escapes, so the body fails to decode ("Invalid \escape") before it
# ever reaches the endpoint. The middleware below escapes lone backslashes so
# such bodies can be processed normally. Already-escaped pairs (\\) and
# unicode escapes (\uXXXX) are preserved.

_LONE_BACKSLASH_RE = re.compile(rb'\\(?!(?!\\|u[0-9a-fA-F]{4}))')


def repair_unescaped_backslashes(raw_body: bytes) -> Optional[bytes]:
    """Return the body with lone backslashes escaped, or None if no repair applies."""
    try:
        json.loads(raw_body)
        return None  # Already valid JSON — nothing to repair.
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    repaired = _LONE_BACKSLASH_RE.sub(rb'\\\\', raw_body)
    try:
        json.loads(repaired)  # Only accept repairs that produce valid JSON.
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return repaired


class JsonPathRepairMiddleware:
    """Repair JSON bodies with unescaped Windows backslash paths before validation."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/process-pdf"
        ):
            buffered: list[Message] = []
            body = b""
            while True:
                message = await receive()
                buffered.append(message)
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break

            repaired = repair_unescaped_backslashes(body)
            if repaired is not None:
                req_id = "INVALID_REQUEST"
                try:
                    req_id = str(json.loads(repaired).get("request_id", req_id))
                except Exception:
                    pass
                RequestLogger(req_id).info(
                    "JSON body contained unescaped backslashes (Windows path); repaired automatically."
                )
                buffered = [{"type": "http.request", "body": repaired, "more_body": False}]

            async def replay_receive() -> Message:
                if buffered:
                    return buffered.pop(0)
                return {"type": "http.disconnect"}

            await self.app(scope, replay_receive, send)
            return

        await self.app(scope, receive, send)


app.add_middleware(JsonPathRepairMiddleware)


class ProcessPdfRequest(BaseModel):
    request_id: str = Field(..., description="Unique non-sequential request identifier") # ... means it is a required field
    pdf_path: str = Field(..., description="Path to PDF file")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "request_id": "",  # [UPDATED] static uuid removed; filled with a fresh UUID on every "Try it out"
                    "pdf_path": "sample.pdf",
                }
            ]
        }
    }

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("request_id cannot be empty or whitespace.")
        if cleaned.isdigit():
            raise ValueError(
                f"Invalid request_id '{v}': must be a unique identifier, not simple sequential numbers like 1, 2, 3..."
            )
        return cleaned

    @field_validator("pdf_path")
    @classmethod
    def validate_pdf_path(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("pdf_path cannot be empty or whitespace.")
        return cleaned


def extract_pdf_metadata(reader: PdfReader) -> Dict[str, Any]:
    """Return raw metadata exactly as stored in the PDF — no substitutions or fallbacks."""
    raw = reader.metadata or {}
    meta = {}
    # Map friendly key → possible PDF key names
    key_map = {
        "title": ["/Title"],
        "author": ["/Author"],
        "subject": ["/Subject"],
        "creator": ["/Creator"],
        "producer": ["/Producer"],
        "creation_date": ["/CreationDate"],
        "modification_date": ["/ModDate"],
    }
    for field, pdf_keys in key_map.items():
        # Prefer the typed attribute on the metadata object (already parsed)
        val = getattr(raw, field, None)
        if val is None:
            # Fall back to raw dict lookup without or-chaining (preserves empty strings)
            for pk in pdf_keys:
                if pk in raw:
                    val = raw[pk]
                    break
        # Convert datetime objects to ISO string; leave everything else as-is
        if hasattr(val, "isoformat"):
            meta[field] = val.isoformat()
        elif val is not None:
            meta[field] = str(val)
        else:
            meta[field] = None
    return meta


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    req_id = "INVALID_REQUEST"
    try:
        body = await request.body()
        if body:
            parsed = json.loads(body.decode("utf-8"))
            req_id = str(parsed.get("request_id", req_id))
    except Exception:
        pass

    logger = RequestLogger(req_id)
    errors = [f"{' -> '.join(str(x) for x in err.get('loc', []))}: {err.get('msg', '')}" for err in exc.errors()]
    msg = "; ".join(errors)
    logger.info(f"Request received with invalid payload: {msg}")
    logger.error(f"RequestValidationError: {msg}")
    logger.info("Response sent: 422 Unprocessable Entity")
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors()), "message": msg})


HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>PDF Processing API - Visual Dashboard</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0b0f19; color: #e2e8f0; margin: 0; padding: 24px; }
    .box { max-width: 900px; margin: 0 auto; background: #151d30; border-radius: 12px; padding: 28px; border: 1px solid #243049; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.5); }
    .top-bar { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #243049; padding-bottom: 16px; margin-bottom: 20px; }
    h1 { margin: 0; font-size: 20px; color: #f8fafc; display: flex; align-items: center; gap: 8px; }
    a { color: #38bdf8; text-decoration: none; font-size: 13px; font-weight: 600; }
    .form-group { margin-bottom: 14px; }
    label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; color: #94a3b8; }
    .input-row { display: flex; gap: 8px; }
    input[type="text"] { flex: 1; padding: 9px 12px; background: #0b0f19; border: 1px solid #334155; border-radius: 6px; color: #fff; font-size: 14px; }
    .preset-row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
    .btn-preset { background: #1e293b; color: #94a3b8; border: 1px solid #334155; padding: 4px 10px; border-radius: 4px; font-size: 12px; cursor: pointer; }
    .btn-preset:hover { background: #334155; color: #fff; }
    .btn-main { width: 100%; margin-top: 14px; padding: 12px; background: #2563eb; color: #fff; border: 0; border-radius: 6px; cursor: pointer; font-weight: 700; font-size: 14px; transition: background 0.15s; }
    .btn-main:hover { background: #1d4ed8; }
    
    /* Visual Result Cards */
    #visualResults { display: none; margin-top: 24px; animation: fadeIn 0.3s ease-in; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
    .stat-hero { display: flex; gap: 20px; align-items: center; background: linear-gradient(135deg, #1e293b, #0f172a); border: 1px solid #38bdf844; border-radius: 10px; padding: 20px; margin-bottom: 16px; }
    .page-stat { text-align: center; min-width: 100px; padding-right: 20px; border-right: 1px solid #334155; }
    .stat-num { font-size: 40px; font-weight: 800; color: #38bdf8; line-height: 1; }
    .stat-lbl { font-size: 11px; font-weight: 700; color: #94a3b8; letter-spacing: 1px; margin-top: 4px; }
    .file-summary h2 { margin: 0 0 6px; font-size: 18px; color: #f8fafc; }
    .file-path { font-size: 12px; color: #64748b; word-break: break-all; font-family: monospace; }
    
    .meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin-bottom: 16px; }
    .meta-card { background: #0e1626; border: 1px solid #243049; border-radius: 8px; padding: 12px 16px; }
    .m-label { font-size: 11px; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
    .m-val { font-size: 14px; color: #f1f5f9; font-weight: 500; word-break: break-word; }

    pre { background: #080c14; padding: 14px; border-radius: 8px; overflow-x: auto; font-size: 12px; max-height: 220px; line-height: 1.5; color: #cbd5e1; border: 1px solid #1e293b; margin-top: 10px; }
    details { margin-top: 12px; }
    summary { cursor: pointer; color: #94a3b8; font-size: 12px; font-weight: 600; }
    input[type="file"] { width: 100%; padding: 8px; background: #0b0f19; border: 1px dashed #334155; border-radius: 6px; color: #94a3b8; font-size: 13px; } /* [ADDED] device upload styling */
  </style>
</head>
<body>
  <div class="box">
    <div class="top-bar">
      <h1><span>📄 PDF Processing API</span></h1>
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
          <button type="button" class="btn-preset" onclick="setPreset('sample_pdf_for_processing_api.pdf')">Sample 1 (Processing API)</button>
          <button type="button" class="btn-preset" onclick="setPreset('sample_pdf_for_stream_extraction.pdf')">Sample 2 (Stream Extraction)</button>
          <button type="button" class="btn-preset" onclick="setPreset('sample.pdf')">Sample 3 (Default)</button>
        </div>
      </div>

      <div class="form-group"> <!-- [ADDED] device upload option -->
        <label>…Or Upload PDF From Your Device</label>
        <input id="fileInput" type="file" accept=".pdf,application/pdf" />
      </div>

      <button id="b" class="btn-main">Process PDF Document</button>
    </form>

    <!-- Visual Extracted Data Section -->
    <div id="visualResults">
      <div class="stat-hero">
        <div class="page-stat">
          <div class="stat-num" id="visPages">0</div>
          <div class="stat-lbl">PAGES</div>
        </div>
        <div class="file-summary">
          <h2 id="visTitle">Document Title</h2>
          <div class="file-path" id="visPath">path/to/document.pdf</div>
        </div>
      </div>

      <div class="meta-grid">
        <div class="meta-card"><div class="m-label">Author</div><div class="m-val" id="visAuthor">-</div></div>
        <div class="meta-card"><div class="m-label">Subject</div><div class="m-val" id="visSubject">-</div></div>
        <div class="meta-card"><div class="m-label">Creator</div><div class="m-val" id="visCreator">-</div></div>
        <div class="meta-card"><div class="m-label">Producer</div><div class="m-val" id="visProducer">-</div></div>
        <div class="meta-card"><div class="m-label">Creation Date</div><div class="m-val" id="visCreated">-</div></div>
        <div class="meta-card"><div class="m-label">Modification Date</div><div class="m-val" id="visModified">-</div></div>
      </div>

      <details>
        <summary>View Raw JSON Response</summary>
        <pre id="rawJson"></pre>
      </details>
    </div>

  </div>

  <script>
    function setPreset(val) {
      document.getElementById('p').value = val;
    }

    function renderValue(id, val) {
      const el = document.getElementById(id);
      // Show whatever the PDF contains. Only show — when the value is truly null/undefined.
      el.textContent = (val !== null && val !== undefined) ? val : '—';
      el.className = 'm-val';
    }

    async function run(e) {
      e.preventDefault();
      const b = document.getElementById('b');
      b.disabled = true; b.textContent = '⏳ Processing PDF...';
      const rid = document.getElementById('rid').value.trim();

      try {
        const fileInput = document.getElementById('fileInput'); // [ADDED] device upload support
        let res;
        if (fileInput.files.length > 0) { // uploaded file takes priority over the path field
          const fd = new FormData();
          fd.append('request_id', rid);
          fd.append('file', fileInput.files[0]);
          res = await fetch('/upload-pdf', { method: 'POST', body: fd });
        } else {
          const p = document.getElementById('p').value.trim();
          res = await fetch('/process-pdf', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ request_id: rid, pdf_path: p })
          });
        }
        const data = await res.json();

        if (res.ok && data.data) {
          document.getElementById('visualResults').style.display = 'block';
          document.getElementById('visPages').textContent = data.data.page_count;
          const meta = data.data.metadata || {};
          // Show title exactly as the PDF stores it; fall back to filename only if genuinely absent
          const titleVal = meta.title;
          document.getElementById('visTitle').textContent =
            (titleVal !== null && titleVal !== undefined) ? titleVal
            : data.data.pdf_path.split(/[\\/]/).pop();
          document.getElementById('visPath').textContent = data.data.pdf_path;
          renderValue('visAuthor', meta.author);
          renderValue('visSubject', meta.subject);
          renderValue('visCreator', meta.creator);
          renderValue('visProducer', meta.producer);
          renderValue('visCreated', meta.creation_date);
          renderValue('visModified', meta.modification_date);
          document.getElementById('rawJson').textContent = JSON.stringify(data, null, 2);
        } else {
          alert('Error: ' + (data.detail || JSON.stringify(data)));
        }
      } catch (err) {
        alert('Request failed: ' + err.message);
      } finally {
        b.disabled = false; b.textContent = 'Process PDF Document';
        genId(); // [ADDED] Auto-generate a fresh request ID after every POST /process-pdf submission
      }
    }
  </script>

  <script type="module">
    // Request IDs via the uuid library (v4) — fresh on every page load/refresh, no Math.random
    // [UPDATED] Expose genId() with the native generator BEFORE the CDN import so the
    // auto-refresh after each submission works immediately, even if the CDN is slow.
    let uuidv4 = () => crypto.randomUUID();

    function genRequestId() {
      return uuidv4();
    }

    function genId() {
      document.getElementById('rid').value = genRequestId();
    }
    window.genId = genId; // for the Regenerate button's inline onclick
    genId();

    // [UPDATED] Upgrade to the uuid library (v4) once loaded; CDN unreachable → keep native generator
    try {
      uuidv4 = (await import('https://cdn.jsdelivr.net/npm/uuid@11.1.0/+esm')).v4;
    } catch {
      // keep crypto.randomUUID() (never Math.random)
    }

    // Tab restored from back/forward cache (e.g. after refresh) → force a brand-new ID
    window.addEventListener('pageshow', e => { if (e.persisted) genId(); });
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
        "endpoints": {"POST /process-pdf": "Process a PDF to extract page count and metadata"},
    }


@app.post("/upload-pdf")  # [ADDED] upload a PDF from the user's device for processing
async def upload_pdf(request_id: str = Form(...), file: UploadFile = File(...)) -> Dict[str, Any]:
    """Accept a PDF file uploaded from the user's device and process it like /process-pdf."""
    req_id = request_id.strip()
    logger = RequestLogger(req_id)
    logger.info(f"Request received (device upload): request_id='{req_id}', filename='{file.filename}'")

    # Same request_id rules as /process-pdf: no trivial sequential IDs like 1, 2, 3
    if req_id.isdigit():
        err = f"Invalid request_id '{request_id}': must be a unique identifier, not simple sequential numbers like 1, 2, 3..."
        logger.error(err)
        logger.info("Response sent: 422 Unprocessable Entity")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err)

    # Uniqueness check (same rule as /process-pdf)
    with _SEEN_LOCK:
        if req_id in _SEEN_REQUEST_IDS:
            err = f"ConflictError: Duplicate request_id: '{req_id}' has already been processed. Each request must have a unique request_id."
            logger.error(err)
            logger.info("Response sent: 409 Conflict")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=err)
        _SEEN_REQUEST_IDS.add(req_id)

    # Upload validation: only real .pdf files
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        err = f"ValueError: Only .pdf files can be uploaded. Got: '{file.filename}'"
        logger.error(err)
        logger.info("Response sent: 400 Bad Request")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)

    # Persist the upload under uploads/ with the request_id prefix (same folder as earlier uploads)
    safe_name = Path(file.filename).name
    upload_dir = Path("uploads")
    upload_dir.mkdir(exist_ok=True)
    upload_path = upload_dir / f"{req_id}_{safe_name}"
    with upload_path.open("wb") as f:
        f.write(await file.read())
    logger.info(f"Uploaded file saved to '{upload_path}'")

    # Process using the same extraction logic as /process-pdf
    try:
        reader = PdfReader(str(upload_path))
        page_count = len(reader.pages)
        metadata = extract_pdf_metadata(reader)
        logger.info(f"Task completed: Successfully processed uploaded '{safe_name}' ({page_count} page(s))")
    except Exception as e:
        err = f"Failed to process uploaded PDF '{safe_name}': {type(e).__name__}: {str(e)}"
        logger.error(err, exc_info=True)
        logger.info("Response sent: 422 Unprocessable Entity")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err)

    logger.info("Response sent: 200 OK")
    return {
        "request_id": req_id,
        "status": "success",
        "data": {
            "pdf_path": str(upload_path.resolve()),
            "page_count": page_count,
            "metadata": metadata,
        },
    }


@app.post("/process-pdf")
async def process_pdf(payload: ProcessPdfRequest) -> Dict[str, Any]:
    req_id, pdf_path = payload.request_id, payload.pdf_path
    logger = RequestLogger(req_id)

    # 1. Request received
    logger.info(f"Request received: request_id='{req_id}', pdf_path='{pdf_path}'")

    # Uniqueness check
    with _SEEN_LOCK:
        if req_id in _SEEN_REQUEST_IDS:
            err = f"ConflictError: Duplicate request_id: '{req_id}' has already been processed. Each request must have a unique request_id."
            logger.error(err)
            logger.info("Response sent: 409 Conflict")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=err)
        _SEEN_REQUEST_IDS.add(req_id)

    # 2. File path validation
    logger.info(f"File path validation started for '{pdf_path}'")
    file_path = Path(pdf_path)

    if not file_path.exists():
        err = f"FileNotFoundError: The PDF file does not exist at '{pdf_path}'"
        logger.error(err)
        logger.info("Response sent: 404 Not Found")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err)

    if not file_path.is_file():
        err = f"IsADirectoryError: Path '{pdf_path}' is a directory, not a file."
        logger.error(err)
        logger.info("Response sent: 400 Bad Request")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)

    if file_path.suffix.lower() != ".pdf":
        err = f"ValueError: Invalid file extension '{file_path.suffix}'. Expected a '.pdf' file."
        logger.error(err)
        logger.info("Response sent: 400 Bad Request")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)

    logger.info(f"File path validation passed: file exists at '{file_path.resolve()}'")

    # 3. PDF opening
    logger.info(f"Opening PDF file '{file_path.name}'")
    try:
        reader = PdfReader(str(file_path.resolve()))
        logger.info("PDF opened successfully")
    except Exception as e:
        err = f"Failed to open PDF file '{pdf_path}': {type(e).__name__}: {str(e)}"
        logger.error(err, exc_info=True)
        logger.info("Response sent: 422 Unprocessable Entity")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err)

    # 4. Task started
    logger.info("Task started: extracting page count and metadata")
    try:
        page_count = len(reader.pages)
        logger.info(f"Page count extracted: {page_count} page(s)")

        metadata = extract_pdf_metadata(reader)
        fields = [k for k, v in metadata.items() if v is not None]
        logger.info(f"Basic metadata extracted: {len(fields)} field(s) populated ({', '.join(fields)})")

        # 5. Task completed
        logger.info(f"Task completed: Successfully processed '{file_path.name}' ({page_count} page(s))")
    except Exception as e:
        err = f"Failed to extract PDF data from '{pdf_path}': {type(e).__name__}: {str(e)}"
        logger.error(err, exc_info=True)
        logger.info("Response sent: 500 Internal Server Error")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=err)

    result = {
        "request_id": req_id,
        "status": "success",
        "data": {
            "pdf_path": str(file_path.resolve()),
            "page_count": page_count,
            "metadata": metadata,
        },
    }

    # 6. Response sent
    logger.info("Response sent: 200 OK")
    return result
