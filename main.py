"""Server entry point for PDF Processing API."""

import uvicorn

from app import app


def main() -> None:
    """Run uvicorn server."""
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    main()
