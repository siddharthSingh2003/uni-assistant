"""Document files and the review queue for student uploads (M4).

* GET /documents/{doc_id}/file serves the stored original of any ingested document.
* A student's upload is never indexed directly: it waits in pending_uploads until an
  admin has looked at the file and approved it. Only then is it ingested, always as
  authority level 5 with a doc_id of its own.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from api.auth import User, current_user, require_admin
from data.db import get_conn, one, rows
from ingestion import document_store, source_register
from ingestion.cleaner import clean_pages
from ingestion.loader import load_document
from ingestion.metadata_extractor import extract_metadata
from ingestion.pipeline import ingest_file
from shared.config import settings
from shared.schemas import IngestMetadata, IngestResponse

log = logging.getLogger("api")
router = APIRouter(tags=["documents"])
ALLOWED_SUFFIXES = set(document_store.CONTENT_TYPES)
UPLOAD_COLS = ("upload_id, student_id, filename, content_type, size, status, submitted_at, reviewed_by, "
               "reviewed_at, note, doc_id")


class ReviewNote(BaseModel):
    note: str = Field(default="", max_length=500)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_upload(filename: str, size: int, limit_mb: Optional[int] = None) -> str:
    """Returns the file suffix, or raises for a type or size the system does not accept."""
    limit_mb = limit_mb or settings.max_upload_mb
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(415, f"Unsupported file type '{suffix}'. Use PDF, TXT, MD or an image.")
    if size == 0:
        raise HTTPException(422, "The file is empty.")
    if size > limit_mb * 1024 * 1024:
        raise HTTPException(413, f"The file is larger than {limit_mb} MB.")
    return suffix


def _file_response(filename: str, content_type: str, content: bytes, download: bool) -> Response:
    return Response(content, media_type=content_type, headers={
        "Content-Disposition": f"{'attachment' if download else 'inline'}; filename*=UTF-8''{quote(filename)}",
        "X-Content-Type-Options": "nosniff"})      # the browser must not guess a type and run the file


def ingest_with_extraction(content: bytes, filename: str, authority_level: int, uploaded_by: str,
                           student_id: Optional[str] = None) -> IngestResponse:
    """Read the metadata from the document, then index it and store the file. With `student_id`
    the document is forced to unofficial and gets a doc_id of its own, so a student upload can
    never take the place of an official document."""
    suffix = Path(filename).suffix.lower()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings.uploads_dir / f"_incoming-{uuid.uuid4().hex}{suffix}"
    tmp.write_bytes(content)
    try:
        text = "\n".join(p.text for p in clean_pages(load_document(tmp)))
        if not text.strip():
            raise ValueError(f"No text could be extracted from {filename} (even with OCR)")
        doc_meta, extracted_by = extract_metadata(text, Path(filename).name, authority_level, uploaded_by=uploaded_by)
        if student_id:
            doc_meta = doc_meta.model_copy(update={"doc_id": f"STU-{student_id}-{doc_meta.doc_id}"[:80],
                                                   "doc_type": "unofficial"})
            extracted_by.update(authority_level="fixed for student uploads", doc_type="fixed for student uploads")
        meta = IngestMetadata(**doc_meta.model_dump())
        dest = settings.uploads_dir / f"{meta.doc_id}{suffix}"
        tmp.replace(dest)
        result = ingest_file(dest, meta, uploaded_by=uploaded_by)
    except ValueError as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(422, str(e))
    result.metadata, result.extracted_by = meta.to_flat(), extracted_by
    source_register.export_csv(settings.storage_dir / "source_register.snapshot.csv")
    return result


def _check_not_spam(student_id: str, content: bytes) -> None:
    """Limits on what one student can put in front of the admins. Checked before anything is stored."""
    pending = one("SELECT COUNT(*) AS n FROM pending_uploads WHERE student_id = ? AND status = 'pending'",
                  (student_id,))["n"]
    if pending >= settings.max_pending_uploads:
        raise HTTPException(429, f"You already have {pending} documents waiting for review. "
                                 "Wait for an admin to review them before adding more.")
    # every submission counts, whatever happened to it: a rejection does not hand the slot back
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    today = one("SELECT COUNT(*) AS n FROM pending_uploads WHERE student_id = ? AND submitted_at >= ?",
                (student_id, since))["n"]
    if today >= settings.max_student_uploads_per_day:
        raise HTTPException(429, f"You can submit {settings.max_student_uploads_per_day} documents in 24 hours. "
                                 "Try again later.")
    # the same file again: already in the portal, already waiting (from anyone), or already refused to this student
    size = len(content)
    if existing := one("SELECT doc_id FROM document_files WHERE size = ? AND content = ?", (size, content)):
        raise HTTPException(409, f"This document is already in the portal as {existing['doc_id']}.")
    if one("SELECT 1 AS x FROM pending_uploads WHERE size = ? AND content = ? AND status = 'pending'", (size, content)):
        raise HTTPException(409, "This document has already been submitted and is waiting for review.")
    if one("SELECT 1 AS x FROM pending_uploads WHERE size = ? AND content = ? AND status = 'rejected' "
           "AND student_id = ?", (size, content, student_id)):
        raise HTTPException(409, "This document was already reviewed and rejected. See the admin's note "
                                 "under My submissions.")


def submit_for_review(student_id: str, filename: str, content: bytes) -> IngestResponse:
    _check_not_spam(student_id, content)
    upload_id = uuid.uuid4().hex[:12]
    name = Path(filename).name
    with get_conn() as c:
        c.execute("INSERT INTO pending_uploads (upload_id, student_id, filename, content_type, size, content, status, "
                  "submitted_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                  (upload_id, student_id, name, document_store.content_type(name), len(content), content, _now()))
    log.info("upload %s submitted for review", upload_id)
    return IngestResponse(doc_id="", chunks_indexed=0, status="pending_review", upload_id=upload_id)


# --------------------------------------------------------------------------- files
@router.get("/documents/{doc_id}/file")
def document_file(doc_id: str, download: bool = False, user: User = Depends(current_user)):
    f = document_store.get(doc_id)
    if f is None:
        raise HTTPException(404, "No stored file for this document.")
    return _file_response(f["filename"], f["content_type"], f["content"], download)


# --------------------------------------------------------------------------- review queue
@router.get("/uploads/mine")
def my_uploads(user: User = Depends(current_user)):
    return rows(f"SELECT {UPLOAD_COLS} FROM pending_uploads WHERE student_id = ? ORDER BY submitted_at DESC",
                (user.id,))


@router.get("/admin/uploads")
def list_uploads(status: Optional[str] = "pending", admin: User = Depends(require_admin)):
    if status in (None, "", "all"):
        return rows(f"SELECT {UPLOAD_COLS} FROM pending_uploads ORDER BY submitted_at DESC")
    return rows(f"SELECT {UPLOAD_COLS} FROM pending_uploads WHERE status = ? ORDER BY submitted_at", (status,))


def _pending(upload_id: str) -> dict:
    up = one("SELECT * FROM pending_uploads WHERE upload_id = ?", (upload_id,))
    if up is None:
        raise HTTPException(404, "upload not found")
    return up


@router.get("/admin/uploads/{upload_id}/file")
def upload_file(upload_id: str, download: bool = False, admin: User = Depends(require_admin)):
    up = _pending(upload_id)
    return _file_response(up["filename"], up["content_type"], up["content"], download)


def _close(upload_id: str, status: str, admin: User, note: str, doc_id: str = "") -> None:
    with get_conn() as c:
        c.execute("UPDATE pending_uploads SET status = ?, reviewed_by = ?, reviewed_at = ?, note = ?, doc_id = ? "
                  "WHERE upload_id = ?", (status, admin.id, _now(), note, doc_id, upload_id))


@router.post("/admin/uploads/{upload_id}/approve", response_model=IngestResponse)
def approve_upload(upload_id: str, body: ReviewNote = ReviewNote(), admin: User = Depends(require_admin)):
    up = _pending(upload_id)
    if up["status"] != "pending":
        raise HTTPException(409, f"This upload was already {up['status']}.")
    result = ingest_with_extraction(up["content"], up["filename"], 5, uploaded_by=up["student_id"],
                                    student_id=up["student_id"])
    _close(upload_id, "approved", admin, body.note, result.doc_id)
    result.upload_id = upload_id
    return result


@router.post("/admin/uploads/{upload_id}/reject")
def reject_upload(upload_id: str, body: ReviewNote = ReviewNote(), admin: User = Depends(require_admin)):
    up = _pending(upload_id)
    if up["status"] != "pending":
        raise HTTPException(409, f"This upload was already {up['status']}.")
    _close(upload_id, "rejected", admin, body.note)
    return {"upload_id": upload_id, "status": "rejected"}
