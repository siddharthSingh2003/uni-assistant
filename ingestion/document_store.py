"""Original document files, kept in SQLite (M1) so the portal can show and download exactly
what was ingested, wherever the file first came from (repository, admin upload, approved
student upload)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from data.db import get_conn, one, rows

log = logging.getLogger(__name__)
CONTENT_TYPES = {".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8",
                 ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".tif": "image/tiff",
                 ".tiff": "image/tiff"}


def content_type(filename: str) -> str:
    return CONTENT_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


def save(doc_id: str, filename: str, content: bytes, uploaded_by: str = "") -> None:
    with get_conn() as c:
        c.execute("INSERT OR REPLACE INTO document_files (doc_id, filename, content_type, size, content, uploaded_by, "
                  "stored_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (doc_id, filename, content_type(filename), len(content), content, uploaded_by,
                   datetime.now(timezone.utc).isoformat(timespec="seconds")))


def get(doc_id: str) -> Optional[dict]:
    return one("SELECT filename, content_type, size, content FROM document_files WHERE doc_id = ?", (doc_id,))


def backfill() -> int:
    """Store the file of every registered document that was ingested before files were kept."""
    missing = rows("SELECT doc_id, file_path FROM source_register WHERE doc_id NOT IN "
                   "(SELECT doc_id FROM document_files)")
    stored = 0
    for r in missing:
        p = Path(r["file_path"] or "")
        if r["file_path"] and p.is_file():
            save(r["doc_id"], p.name, p.read_bytes(), "repository")
            stored += 1
        else:
            log.warning("no file to store for %s (%s)", r["doc_id"], r["file_path"])
    return stored
