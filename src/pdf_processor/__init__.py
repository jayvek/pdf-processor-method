"""PDF Processor package."""

from app import app
from logger import RequestLogger, configure_logging


def main() -> None:
    """Entry point for CLI command."""
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)


__all__ = ["app", "RequestLogger", "configure_logging", "main"]
