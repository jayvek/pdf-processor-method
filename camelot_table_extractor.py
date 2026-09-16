"""Dual-mode PDF Table Extractor: Camelot (pre-lined) + Tabula dynamic grid (borderless invoices)."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Union

import camelot
import pandas as pd
import pymupdf
from pypdf import PdfReader
import tabula

DEFAULT_TABLES_DIR = Path("data/extracted_tables")
DEFAULT_JSON_PATH = Path("data/extracted_tables/extracted_tables.json")
_APPEND_LOCK = threading.Lock()

HEADER_KEYWORDS = ("item", "description", "quantity", "qty", "rate", "price", "amount", "unit price", "cost")
SUMMARY_KEYWORDS = ("subtotal", "total", "discount", "shipping", "balance due", "terms", "notes", "tax", "thank you", "paid")


def _clean_cell(val: Any) -> str:
    """Normalize cell value: strip linebreaks, carriage returns, and extra whitespace."""
    if val is None or pd.isna(val):
        return ""
    s = str(val).strip()
    return "" if s.lower() == "nan" else " ".join(s.split())


def _rows_to_key_value_records(table: Union[List[List[Any]], pd.DataFrame]) -> List[Dict[str, str]]:
    """Convert a 2D table grid or DataFrame to key/value dictionaries."""
    if hasattr(table, "values"):
        cols = [_clean_cell(c) for c in table.columns]
        raw_rows = [cols] + table.values.tolist() if any(c and not c.startswith("Unnamed:") for c in cols) else table.values.tolist()
    elif isinstance(table, list):
        raw_rows = table
    else:
        return []

    if not raw_rows or len(raw_rows) < 2:
        return []

    headers = [(i, _clean_cell(c)) for i, c in enumerate(raw_rows[0]) if _clean_cell(c)]
    if not headers:
        return []

    records = []
    for row in raw_rows[1:]:
        row_dict = {k: _clean_cell(row[i]) if i < len(row) else "" for i, k in headers}
        first_val = list(row_dict.values())[0].lower() if row_dict else ""
        if any(row_dict.values()) and not any(first_val.startswith(sk) for sk in SUMMARY_KEYWORDS):
            records.append(row_dict)
    return records


def add_table_grid_lines_to_pdf(doc: pymupdf.Document) -> bool:
    """Detect open/borderless tables and draw ruling grid lines for Tabula lattice extraction."""
    lines_added = False
    for page in doc:
        words = page.get_text("words")
        if not words:
            continue
        h_words = [w for w in words if w[4].lower().strip(":") in HEADER_KEYWORDS]
        if not h_words:
            continue

        h_tops = sorted({round(w[1], 1) for w in h_words})
        line_h_words = [w for w in h_words if abs(w[1] - h_tops[0]) < 8.0]
        if len(line_h_words) < 2:
            continue

        h_y0, h_y1 = min(w[1] for w in line_h_words) - 5.0, max(w[3] for w in line_h_words) + 5.0
        s_words = [w for w in words if any(w[4].lower().startswith(sk) for sk in SUMMARY_KEYWORDS) and w[1] > h_y1]
        bot_y = min(w[1] for w in s_words) - 6.0 if s_words else page.rect.y1 - 40.0

        drawings = page.get_drawings()
        h_drawings = [d["rect"] for d in drawings if abs(d["rect"].y0 - h_y0) < 20.0 or abs(d["rect"].y1 - h_y1) < 20.0]
        if h_drawings:
            x_coords = sorted({round(r.x0, 1) for r in h_drawings} | {round(r.x1, 1) for r in h_drawings})
        else:
            sorted_hw = sorted(line_h_words, key=lambda w: w[0])
            x_coords = [page.rect.x0 + 20.0] + [round((sorted_hw[i][2] + sorted_hw[i + 1][0]) / 2.0, 1) for i in range(len(sorted_hw) - 1)] + [page.rect.x1 - 20.0]

        shape = page.new_shape()
        min_x, max_x = x_coords[0], x_coords[-1]
        for y in (h_y0, h_y1, bot_y):
            shape.draw_line(pymupdf.Point(min_x, y), pymupdf.Point(max_x, y))
        for x in x_coords:
            shape.draw_line(pymupdf.Point(x, h_y0), pymupdf.Point(x, bot_y))
        shape.finish(color=(0, 0, 0), width=1.0)
        shape.commit()
        lines_added = True
    return lines_added


def _extract_tabula_with_grid(path_obj: Path) -> List[Dict[str, Any]]:
    """Fallback: draw grid lines on open invoices and extract using Tabula lattice."""
    try:
        doc = pymupdf.open(str(path_obj.resolve()))
        if doc.is_encrypted:
            try:
                doc.authenticate("")
            except Exception:
                pass
        add_table_grid_lines_to_pdf(doc)
    except Exception:
        return []

    temp_pdf = NamedTemporaryFile(suffix=".pdf", delete=False)
    temp_pdf_path = Path(temp_pdf.name)
    temp_pdf.close()

    try:
        doc.save(str(temp_pdf_path))
        doc.close()
        dfs = tabula.read_pdf(str(temp_pdf_path.resolve()), pages="all", lattice=True, multiple_tables=True)
    except Exception:
        dfs = []
    finally:
        temp_pdf_path.unlink(missing_ok=True)

    tables = []
    for idx, df in enumerate(dfs):
        rows = _rows_to_key_value_records(df)
        if rows:
            tables.append({"page": 1, "table_index_on_page": idx, "rows": rows})
    return tables


def extract_tables_from_pdf(pdf_path: str, flavor: str = "lattice") -> List[Dict[str, Any]]:
    """Extract tables from PDF. Pre-lined tables use Camelot lattice; open invoices fall back to Tabula grid."""
    path_obj = Path(pdf_path)
    if not path_obj.exists() or not path_obj.is_file():
        return []

    try:
        reader = PdfReader(str(path_obj.resolve()))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass
        _ = len(reader.pages)
    except Exception:
        return []

    # 1. Try Camelot lattice extraction for PDFs with existing grid lines
    all_tables: List[Dict[str, Any]] = []
    try:
        camelot_tables = camelot.read_pdf(str(path_obj.resolve()), pages="all", flavor=flavor)
        for idx, table in enumerate(camelot_tables):
            rows = _rows_to_key_value_records(table.df)
            if rows:
                order = getattr(table, "order", None)
                table_idx = (order - 1) if isinstance(order, int) and order > 0 else idx
                all_tables.append({"page": int(getattr(table, "page", 1)), "table_index_on_page": table_idx, "rows": rows})
    except Exception:
        all_tables = []

    # 2. Fallback to Tabula with dynamic grid-lining for borderless invoices
    if not all_tables:
        all_tables = _extract_tabula_with_grid(path_obj)

    return all_tables


def save_tables_to_individual_jsons(
    pdf_path: str,
    request_id: str,
    tables: List[Dict[str, Any]],
    output_dir: Optional[Union[str, Path]] = None,
) -> List[Path]:
    """Save EVERY extracted table into its own separate, distinct JSON file."""
    target_dir = Path(output_dir) if output_dir is not None else DEFAULT_TABLES_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved_pdf = str(Path(pdf_path).resolve())
    safe_stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in (Path(pdf_path).stem or "doc"))

    created_files: List[Path] = []
    with _APPEND_LOCK:
        for idx, table in enumerate(tables, start=1):
            page_num = table.get("page", 1)
            table_idx = table.get("table_index_on_page", 0) + 1
            json_path = target_dir / f"{request_id}_{safe_stem}_table_{idx}_p{page_num}_t{table_idx}.json"
            data = {
                "request_id": request_id,
                "pdf_path": resolved_pdf,
                "extracted_at": datetime.now(timezone.utc).isoformat(),
                "table_number": idx,
                "page": page_num,
                "table_index_on_page": table_idx,
                "row_count": len(table.get("rows", [])),
                "rows": table.get("rows", []),
            }
            json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            created_files.append(json_path)

    return created_files


def append_tables_to_json(
    pdf_path: str,
    request_id: str,
    tables: List[Dict[str, Any]],
    json_path: Optional[Union[str, Path]] = None,
) -> List[Path]:
    """Compatibility wrapper that saves each table into an individual JSON file."""
    out_dir = Path(json_path).parent if json_path and str(json_path).endswith(".json") else json_path
    return save_tables_to_individual_jsons(pdf_path, request_id, tables, output_dir=out_dir)


def extract_and_store_tables(
    pdf_path: str,
    request_id: str,
    json_path: Optional[Union[str, Path]] = None,
    flavor: str = "lattice",
) -> Dict[str, Any]:
    """Extract tables from PDF and save each table to an individual JSON file."""
    tables = extract_tables_from_pdf(pdf_path, flavor=flavor)
    out_dir = Path(json_path).parent if json_path and str(json_path).endswith(".json") else json_path
    created_files = save_tables_to_individual_jsons(pdf_path, request_id, tables, output_dir=out_dir)
    return {
        "table_count": len(tables),
        "row_count": sum(len(t.get("rows", [])) for t in tables),
        "json_files": [str(p.resolve()) for p in created_files],
        "tables": tables,
    }
