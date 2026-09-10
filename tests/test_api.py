"""Comprehensive test suite for PDF Processing API and Logger."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import app
from logger import DEFAULT_ERROR_LOG, DEFAULT_STATUS_LOG, RequestLogger, configure_logging


@pytest.fixture(autouse=True)
def setup_logs():
    """Ensure logging is fresh for each test run."""
    configure_logging(force_reconfigure=True)


@pytest.fixture
def client():
    return TestClient(app)


def test_root_endpoint(client):
    """Verify health / root endpoint returns JSON for API clients."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "PDF Processing API"


def test_html_dashboard_in_browser(client):
    """Verify root endpoint returns interactive HTML dashboard when visited by a browser."""
    response = client.get("/", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "PDF Processing API" in response.text
    assert "Process PDF Document" in response.text


def test_process_valid_pdf(client):
    """Verify successful processing of sample.pdf and complete stage logging."""
    req_id = "req-test-success-001"
    response = client.post("/process-pdf", json={"request_id": req_id, "pdf_path": "sample.pdf"})
    assert response.status_code == 200
    data = response.json()
    assert data["request_id"] == req_id
    assert data["data"]["page_count"] == 3
    assert data["data"]["metadata"]["title"] == "Annual Technical Report 2026"

    # Verify status.log contains all required stages
    status_content = Path(DEFAULT_STATUS_LOG).read_text(encoding="utf-8")
    for stage in ["Request received", "File path validation passed", "PDF opened successfully", "Task started", "Task completed", "Response sent"]:
        assert stage in status_content
    assert req_id in status_content


def test_file_not_found(client):
    """Verify 404 response and error logging with line number when file is missing."""
    req_id = "req-test-not-found-404"
    res = client.post("/process-pdf", json={"request_id": req_id, "pdf_path": "missing_123.pdf"})
    assert res.status_code == 404

    error_content = Path(DEFAULT_ERROR_LOG).read_text(encoding="utf-8")
    assert req_id in error_content
    assert "FileNotFoundError" in error_content
    assert "app.py:" in error_content  # line number check


def test_invalid_extension(client):
    """Verify 400 error when given a non-PDF file."""
    req_id = "req-test-invalid-ext"
    res = client.post("/process-pdf", json={"request_id": req_id, "pdf_path": "README.md"})
    assert res.status_code == 400
    assert "Expected a '.pdf' file" in res.json()["detail"]


def test_corrupted_pdf(client, tmp_path):
    """Verify 422 error when file is corrupted."""
    corrupt_file = tmp_path / "corrupted.pdf"
    corrupt_file.write_bytes(b"NOT A REAL PDF FILE")

    req_id = "req-test-corrupt-001"
    res = client.post("/process-pdf", json={"request_id": req_id, "pdf_path": str(corrupt_file)})
    assert res.status_code == 422

    error_content = Path(DEFAULT_ERROR_LOG).read_text(encoding="utf-8")
    assert req_id in error_content
    assert "Failed to open PDF file" in error_content


def test_duplicate_request_id(client):
    """Verify 409 conflict when request_id is reused."""
    req_id = "req-unique-id-999"
    assert client.post("/process-pdf", json={"request_id": req_id, "pdf_path": "sample.pdf"}).status_code == 200
    res2 = client.post("/process-pdf", json={"request_id": req_id, "pdf_path": "sample.pdf"})
    assert res2.status_code == 409
    assert "Duplicate request_id" in res2.json()["detail"]


def test_trivial_sequential_request_id(client):
    """Verify rejection of trivial sequential request IDs like '1', '2', '3'."""
    for bad_id in ["1", "2", "3", "007"]:
        res = client.post("/process-pdf", json={"request_id": bad_id, "pdf_path": "sample.pdf"})
        assert res.status_code == 422
        assert "must be a unique identifier" in res.text


def test_repair_unescaped_backslashes_helper():
    """Repair helper escapes lone backslashes (Windows paths) and leaves valid JSON untouched."""
    from app import repair_unescaped_backslashes

    # Raw body with literal single backslashes — invalid JSON ("Invalid \\escape")
    raw = b'{"request_id": "g", "pdf_path": "C:\\Users\\Asus\\report.pdf"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)

    repaired = repair_unescaped_backslashes(raw)
    assert repaired is not None
    assert json.loads(repaired)["pdf_path"] == "C:\\Users\\Asus\\report.pdf"

    # Valid-but-ambiguous escapes (\t, \n, \r) meant literally in Windows paths
    # must be escaped too when the body is otherwise invalid — here \\U makes
    # the body fail to parse, so all lone backslashes get escaped.
    raw2 = b'{"request_id": "g", "pdf_path": "C:\\tests\\Users\\notes.pdf"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw2)
    repaired2 = repair_unescaped_backslashes(raw2)
    assert repaired2 is not None
    assert json.loads(repaired2)["pdf_path"] == "C:\\tests\\Users\\notes.pdf"

    # Already-valid JSON (escaped backslashes) must be left untouched
    valid = b'{"request_id": "g", "pdf_path": "C:\\\\Users\\\\Asus\\\\report.pdf"}'
    assert repair_unescaped_backslashes(valid) is None


def test_process_pdf_with_unescaped_windows_path(client, tmp_path, monkeypatch):
    """Raw JSON bodies with unescaped Windows backslash paths are repaired, not rejected."""
    # On Windows, '.\\sample.pdf' == tmp_path/sample.pdf; on POSIX it is a file
    # literally named '.\sample.pdf'. Either way the repaired path exists.
    shutil.copyfile("sample.pdf", tmp_path / ".\\sample.pdf")
    monkeypatch.chdir(tmp_path)

    raw_body = b'{"request_id": "req-backslash-001", "pdf_path": ".\\sample.pdf"}'
    res = client.post(
        "/process-pdf",
        content=raw_body,
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["request_id"] == "req-backslash-001"
    assert data["data"]["page_count"] == 3
    assert data["data"]["metadata"]["title"] == "Annual Technical Report 2026"

    status_content = (Path(__file__).resolve().parent.parent / DEFAULT_STATUS_LOG).read_text(
        encoding="utf-8"
    )
    assert "repaired automatically" in status_content


def test_process_pdf_still_rejects_broken_json(client):
    """Genuinely malformed JSON (not just backslash escapes) still returns 422."""
    res = client.post(
        "/process-pdf",
        content=b'{"request_id": "req-broken-json", "pdf_path": }',
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 422
    assert "json_invalid" in res.text


def test_logger_line_numbers_behavior(tmp_path):
    """Verify that INFO logs omit line numbers while ERROR logs include line numbers."""
    sf, ef = tmp_path / "test_status.log", tmp_path / "test_error.log"
    configure_logging(status_log_path=sf, error_log_path=ef, enable_console=False, force_reconfigure=True)

    test_logger = RequestLogger("req-line-check")
    test_logger.info("Informational message without line number")
    test_logger.error("Error message with line number")

    status_text = sf.read_text(encoding="utf-8")
    error_text = ef.read_text(encoding="utf-8")

    assert "[test_api.py] Informational message without line number" in status_text
    assert "test_api.py:" in error_text  # Contains line number
    assert "Error message with line number" in error_text

    configure_logging(force_reconfigure=True)
