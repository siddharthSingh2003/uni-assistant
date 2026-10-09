"""FastAPI app (M4) implementing the fixed API contract in Section 6 of the guide.

    uvicorn api.main:app --reload
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from pydantic import ValidationError

from api import audit
from api.auth import User, current_user, require_admin, router as auth_router
from api.documents import check_upload, ingest_with_extraction, router as documents_router, submit_for_review
from data import credentials
from data.db import init_db, one
from data.loader import LoadError, load_dir
from data.validate import REQUIRED
from graph.workflow import run_question
from ingestion import document_store, source_register
from ingestion.pipeline import bootstrap, ingest_file
from ingestion.vector_store import get_store
from shared.config import RetrievalConfig, settings
from shared.llm import LLMClient
from shared.schemas import AskRequest, AskResponse, IngestMetadata, IngestResponse, RuleIn
from tools.rules import add_rule, load_rules_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        log.info("bootstrap documents: %s", bootstrap(settings.register_csv))
        log.info("seed rules: %s", load_rules_csv(settings.rules_seed_csv))
        if not one("SELECT 1 AS x FROM students LIMIT 1") and (settings.students_dir / "students.csv").exists():
            log.info("seed students: %s", load_dir(settings.students_dir)["loaded"])
        log.info("credentials initialised for %d student(s)", credentials.ensure_credentials())
        if credentials.ensure_admin():
            log.info("created admin account '%s'", settings.admin_username)
        log.info("document files stored for %d earlier document(s)", document_store.backfill())
    except Exception as e:  # noqa: BLE001 - the API must still start and report via /health
        log.error("startup seeding failed: %s", e)
    yield


app = FastAPI(title="University Student Services Assistant", version="1.0.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(documents_router)


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, user: User = Depends(current_user)):
    # an admin is not a student: they can ask about the documents, but there are no "my records"
    response, record = run_question(req.question, user.student_id, req.as_of_date)
    audit.save(record)
    # R7: operational log carries no personal data, only the trace
    log.info("trace=%s type=%s latency_ms=%s", record["trace_id"], record["answer_type"], record["latency_ms"])
    return response


@app.post("/ingest", response_model=IngestResponse)
def ingest(response: Response, file: UploadFile = File(...), metadata: Optional[str] = Form(default=None),
           authority_level: Optional[int] = Form(default=None, ge=1, le=5), user: User = Depends(current_user)):
    """An admin sends either `authority_level` alone (the rest of the metadata is read from the
    document) or the full `metadata` JSON of the original contract; the document is indexed at once.
    A student's upload is NOT indexed: it is held for an admin to review (202, status
    pending_review) and, if approved, is ingested as level 5 (unofficial)."""
    is_staff = user.is_admin or not settings.auth_required      # legacy contract has no roles
    content = file.file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    filename = Path(file.filename or "").name
    suffix = check_upload(filename, len(content), None if is_staff else settings.max_student_upload_mb)
    if not is_staff:
        if metadata is not None:
            raise HTTPException(403, "Only an admin can supply metadata. Upload the file alone.")
        response.status_code = 202
        return submit_for_review(user.id, filename, content)
    if metadata is None:
        if authority_level is None:
            raise HTTPException(422, "send authority_level (metadata is then extracted) or the full metadata JSON")
        return ingest_with_extraction(content, filename, authority_level, uploaded_by=user.id or "legacy")
    try:
        meta = IngestMetadata.model_validate(json.loads(metadata))
    except (json.JSONDecodeError, ValidationError) as e:
        raise HTTPException(422, f"invalid metadata: {e}")
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = settings.uploads_dir / f"{meta.doc_id}{suffix}"
    dest.write_bytes(content)
    try:
        result = ingest_file(dest, meta, uploaded_by=user.id or "legacy")
    except ValueError as e:
        raise HTTPException(422, str(e))
    result.metadata = meta.to_flat()
    added = 0
    for r in meta.rules:
        add_rule(r, origin="ingest")
        added += 1
    result.rules_added = added
    source_register.export_csv(settings.storage_dir / "source_register.snapshot.csv")
    return result


@app.get("/health")
def health():
    status = {"api": "ok"}
    try:
        status["vector_store"] = {"status": "ok", "chunks": get_store(RetrievalConfig.from_env()).count()}
    except Exception as e:  # noqa: BLE001
        status["vector_store"] = {"status": "down", "error": str(e)}
    try:
        status["sqlite"] = {"status": "ok",
                            "students": one("SELECT COUNT(*) AS n FROM students")["n"],
                            "rules": one("SELECT COUNT(*) AS n FROM rule_registry")["n"],
                            "documents": one("SELECT COUNT(*) AS n FROM source_register")["n"]}
    except Exception as e:  # noqa: BLE001
        status["sqlite"] = {"status": "down", "error": str(e)}
    status["llm"] = LLMClient().health()
    healthy = all(v == "ok" or (isinstance(v, dict) and v.get("status") == "ok") for v in status.values())
    return {"status": "ok" if healthy else "degraded", **status}


@app.get("/audit/{trace_id}")
def get_audit(trace_id: str, admin: User = Depends(require_admin)):
    """Admin only: a trace holds a student's records and the system's internal reasoning."""
    rec = audit.get(trace_id)
    if rec is None:
        raise HTTPException(404, "trace_id not found")
    return rec


@app.get("/sources")
def sources():
    return source_register.all_rows()


@app.post("/admin/load-students")
def load_students(files: list[UploadFile] = File(...), admin: User = Depends(require_admin)):
    """Test-student loader endpoint: upload any of students/courses/attendance/results.csv."""
    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            name = Path(f.filename or "").name
            if name.removesuffix(".csv") not in (*REQUIRED, "credentials"):
                raise HTTPException(422, f"unexpected file {name}; expected {[t + '.csv' for t in REQUIRED]}")
            with (Path(tmp) / name).open("wb") as fh:
                shutil.copyfileobj(f.file, fh)
        try:
            return load_dir(tmp)
        except LoadError as e:
            raise HTTPException(422, e.report)


@app.post("/admin/rules")
def add_rules(rules: list[RuleIn], admin: User = Depends(require_admin)):
    try:
        for r in rules:
            add_rule(r, origin="manual")
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"added": len(rules)}
