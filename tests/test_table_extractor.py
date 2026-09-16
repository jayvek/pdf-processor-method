"""Tests for lined-table extraction (table_extractor.py) and its API integration."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import table_extractor
from app import app
from logger import configure_logging
from table_extractor import (
    _rows_to_key_value_records,
    append_tables_to_json,
    extract_and_store_tables,
    extract_tables_from_pdf,
)


@pytest.fixture(autouse=True)
def _fresh_logs():
    """Keep logging handlers fresh for each test."""
    configure_logging(force_reconfigure=True)


@pytest.fixture(autouse=True)
def _isolated_tables_file(tmp_path, monkeypatch):
    """Point the shared JSON output file at a per-test temp location."""
    target = tmp_path / "extracted_tables.json"
    monkeypatch.setattr(table_extractor, "DEFAULT_JSON_PATH", target)
    yield target


@pytest.fixture
def client():
    return TestClient(app)


def _make_lined_table_pdf(path: Path) -> None:
    """Build a minimal valid PDF containing one ruled 3x4 table (header + 3 rows)."""
    x0, y0 = 72, 600
    col_w, row_h = 120, 24
    header = ["Name", "Qty", "Price"]
    data_rows = [
        ["Widget", "5", "9.99"],
        ["Gadget", "2", "19.50"],
        ["Bolt", "12", "0.35"],
    ]

    ops = ["0.5 w"]
    for i in range(len(header) + 1):  # vertical ruling lines
        x = x0 + i * col_w
        ops.append(f"{x} {y0} m {x} {y0 + (len(data_rows) + 1) * row_h} l S")
    for j in range(len(data_rows) + 2):  # horizontal ruling lines
        y = y0 + j * row_h
        ops.append(f"{x0} {y} m {x0 + len(header) * col_w} {y} l S")

    for r, row in enumerate([header] + data_rows):
        for c, cell in enumerate(row):
            tx = x0 + c * col_w + 5
            ty = y0 + (len(data_rows) - r) * row_h + 8  # header row is topmost
            escaped = cell.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            ops.append(f"BT /F1 10 Tf {tx} {ty} Td ({escaped}) Tj ET")

    content = "\n".join(ops).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    path.write_bytes(bytes(out))


def test_rows_to_key_value_records_header_becomes_keys():
    """First row supplies keys; blank rows are skipped; unnamed columns are dropped."""
    table = [
        ["Name", "Qty", None],
        ["Widget", "5", "no-header-column"],
        ["", "", ""],
    ]
    assert _rows_to_key_value_records(table) == [{"Name": "Widget", "Qty": "5"}]


def test_rows_to_key_value_records_empty_inputs():
    assert _rows_to_key_value_records([]) == []
    assert _rows_to_key_value_records([[None, ""]]) == []


def test_extract_tables_from_real_lined_pdf(tmp_path):
    pdf = tmp_path / "lined.pdf"
    _make_lined_table_pdf(pdf)
    tables = extract_tables_from_pdf(str(pdf))
    assert len(tables) == 1
    entry = tables[0]
    assert entry["page"] == 1
    assert entry["table_index_on_page"] == 0
    assert entry["rows"] == [
        {"Name": "Widget", "Qty": "5", "Price": "9.99"},
        {"Name": "Gadget", "Qty": "2", "Price": "19.50"},
        {"Name": "Bolt", "Qty": "12", "Price": "0.35"},
    ]


def test_append_tables_replaces_entry_for_same_pdf(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-fake")
    target = tmp_path / "out.json"
    tables = [{"page": 1, "table_index_on_page": 0, "rows": [{"A": "1"}]}]

    append_tables_to_json(str(pdf), "req-1", tables, target)
    append_tables_to_json(str(pdf), "req-2", tables, target)

    entries = json.loads(target.read_text(encoding="utf-8"))
    assert len(entries) == 1  # same pdf_path → replaced, not duplicated
    assert entries[0]["request_id"] == "req-2"
    assert entries[0]["table_count"] == 1
    assert entries[0]["extracted_at"]


def test_append_tables_accumulates_multiple_pdfs(tmp_path):
    target = tmp_path / "out.json"
    t = [{"page": 1, "table_index_on_page": 0, "rows": [{"K": "V"}]}]
    append_tables_to_json(str(tmp_path / "a.pdf"), "req-a", t, target)
    append_tables_to_json(str(tmp_path / "b.pdf"), "req-b", t, target)
    entries = json.loads(target.read_text(encoding="utf-8"))
    assert [e["request_id"] for e in entries] == ["req-a", "req-b"]


def test_corrupt_existing_json_starts_fresh(tmp_path):
    target = tmp_path / "out.json"
    target.write_text("{not valid json", encoding="utf-8")
    t = [{"page": 1, "table_index_on_page": 0, "rows": [{"K": "V"}]}]
    append_tables_to_json(str(tmp_path / "a.pdf"), "req-a", t, target)
    entries = json.loads(target.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["request_id"] == "req-a"


def test_extract_and_store_tables_pipeline(tmp_path):
    pdf = tmp_path / "lined.pdf"
    _make_lined_table_pdf(pdf)
    target = tmp_path / "store.json"

    summary = extract_and_store_tables(str(pdf), "req-pipe", target)

    assert summary["table_count"] == 1
    assert summary["row_count"] == 3
    assert Path(summary["json_file"]) == target.resolve()
    stored = json.loads(target.read_text(encoding="utf-8"))
    assert stored[0]["request_id"] == "req-pipe"


def test_process_pdf_includes_tables_and_appends_json(client, tmp_path):
    pdf = tmp_path / "lined.pdf"
    _make_lined_table_pdf(pdf)

    res = client.post("/process-pdf", json={"request_id": "req-tables-001", "pdf_path": str(pdf)})
    assert res.status_code == 200
    tables_summary = res.json()["data"]["tables"]
    assert tables_summary["table_count"] == 1
    assert tables_summary["row_count"] == 3
    assert tables_summary["tables"][0]["rows"][0]["Name"] == "Widget"

    entries = json.loads(table_extractor.DEFAULT_JSON_PATH.read_text(encoding="utf-8"))
    assert entries[0]["request_id"] == "req-tables-001"
    assert entries[0]["pdf_path"] == str(pdf.resolve())


def test_upload_pdf_also_extracts_tables(client, tmp_path):
    pdf = tmp_path / "upload_lined.pdf"
    _make_lined_table_pdf(pdf)
    with pdf.open("rb") as f:
        res = client.post(
            "/upload-pdf",
            data={"request_id": "req-upload-tables-001"},
            files={"file": ("upload_lined.pdf", f, "application/pdf")},
        )
    assert res.status_code == 200
    tables_summary = res.json()["data"]["tables"]
    assert tables_summary["table_count"] == 1
    assert tables_summary["row_count"] == 3


def test_pdf_without_tables_reports_zero(client, tmp_path):
    from pypdf import PdfWriter

    pdf = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with pdf.open("wb") as f:
        writer.write(f)

    res = client.post("/process-pdf", json={"request_id": "req-no-tables-001", "pdf_path": str(pdf)})
    assert res.status_code == 200
    t = res.json()["data"]["tables"]
    assert t == {"json_file": t["json_file"], "table_count": 0, "row_count": 0, "tables": []}

    entries = json.loads(table_extractor.DEFAULT_JSON_PATH.read_text(encoding="utf-8"))
    assert len(entries) == 1  # entry still recorded, with zero tables


def test_extracted_tables_endpoint_returns_entries(client, tmp_path):
    pdf = tmp_path / "lined.pdf"
    _make_lined_table_pdf(pdf)
    client.post("/process-pdf", json={"request_id": "req-tables-002", "pdf_path": str(pdf)})

    res = client.get("/extracted-tables")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert len(body["entries"]) == 1
    assert body["entries"][0]["request_id"] == "req-tables-002"


def test_extracted_tables_endpoint_empty_when_no_file(client):
    res = client.get("/extracted-tables")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["entries"] == []
