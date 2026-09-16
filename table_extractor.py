"""Lined-table extraction from PDFs into key/value JSON records."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd
import tabula
from pypdf import PdfReader

DEFAULT_TABLES_DIR = Path("data/extracted_tables")
DEFAULT_JSON_PATH = Path("data/extracted_tables/extracted_tables.json")
_APPEND_LOCK = threading.Lock()


def _clean_cell(val: Any) -> str:
    """Normalize a table cell value: strip linebreaks, carriage returns, and extra spaces."""
    if val is None or pd.isna(val):
        return ""
    s = str(val).strip()
    if s.lower() == "nan":
        return ""
    return " ".join(s.split())


def _rows_to_key_value_records(
    table: Union[List[List[Any]], pd.DataFrame]
) -> List[Dict[str, str]]:
    """Convert a 2D table grid or DataFrame to key/value dictionaries."""
    if hasattr(table, "values"):
        cols = [_clean_cell(c) for c in table.columns]
        if any(not c.startswith("Unnamed:") and c != "" for c in cols):
            raw_rows = [cols] + table.values.tolist()
        else:
            raw_rows = table.values.tolist()
    elif isinstance(table, list):
        raw_rows = table
    else:
        return []

    if not raw_rows or len(raw_rows) < 2:
        return []

    header_row = raw_rows[0]
    headers: List[tuple[int, str]] = [
        (i, _clean_cell(c)) for i, c in enumerate(header_row) if _clean_cell(c)
    ]
    if not headers:
        return []

    records: List[Dict[str, str]] = []
    for row in raw_rows[1:]:
        row_dict = {
            k: _clean_cell(row[i]) if i < len(row) else "" for i, k in headers
        }
        if any(row_dict.values()):
            records.append(row_dict)
    return records


def extract_tables_from_pdf(pdf_path: str) -> List[Dict[str, Any]]:
    """Extract all lined tables from a PDF into page-indexed record lists."""
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
        total_pages = len(reader.pages)
    except Exception:
        return []

    all_tables: List[Dict[str, Any]] = []
    for page_num in range(1, total_pages + 1):
        try:
            dfs = tabula.read_pdf(
                str(path_obj.resolve()),
                pages=page_num,
                lattice=True,
                multiple_tables=True,
            )
        except Exception:
            dfs = []

        for idx, df in enumerate(dfs):
            rows = _rows_to_key_value_records(df)
            if rows:
                all_tables.append({
                    "page": page_num,
                    "table_index_on_page": idx,
                    "rows": rows,
                })
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
    pdf_stem = Path(pdf_path).stem or "doc"
    safe_stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in pdf_stem)

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
) -> Dict[str, Any]:
    """Extract tables from PDF and save each table to an individual JSON file."""
    tables = extract_tables_from_pdf(pdf_path)
    out_dir = Path(json_path).parent if json_path and str(json_path).endswith(".json") else json_path
    created_files = save_tables_to_individual_jsons(pdf_path, request_id, tables, output_dir=out_dir)
    return {
        "table_count": len(tables),
        "row_count": sum(len(t.get("rows", [])) for t in tables),
        "json_files": [str(p.resolve()) for p in created_files],
        "tables": tables,
    }