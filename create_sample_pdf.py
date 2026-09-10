"""Generate a sample PDF file with metadata and multiple pages for testing."""

from pathlib import Path
from pypdf import PdfWriter


def generate_sample_pdf(output_path: str = "sample.pdf") -> Path:
    target = Path(output_path).resolve()
    writer = PdfWriter()

    # Add 3 sample blank pages
    for i in range(3):
        writer.add_blank_page(width=612, height=792)  # Standard Letter size

    # Attach standard metadata
    writer.add_metadata(
        {
            "/Title": "Annual Technical Report 2026",
            "/Author": "Engineering Team",
            "/Subject": "PDF Processing System Verification",
            "/Creator": "PDF Processor API Generator",
            "/Producer": "PyPDF 6.x",
            "/Keywords": "pdf, processing, logging, fastapi",
        }
    )

    with open(target, "wb") as f:
        writer.write(f)

    print(f"Sample PDF successfully generated at: {target}")
    return target


if __name__ == "__main__":
    generate_sample_pdf()
