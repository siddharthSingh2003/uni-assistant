# Team roles and contribution statement

Each member owns one layer end to end: its code, its tests, its share of the README, and its part of the
demo. Interfaces between layers live in `shared/` and were agreed at kickoff, so all four could build in
parallel against mocks. Replace `Member N` with real names before submission.

## Member 1: Knowledge and Retrieval Engineer

**Owns:** `ingestion/`, `docs/`, `tests/test_ingestion.py`, `tests/test_precedence.py`

**What they build**
- The ingestion pipeline: PDF text with PyMuPDF, OCR fallback for scanned pages, table extraction with
  pdfplumber, cleaning of running headers and page numbers (`loader.py`, `cleaner.py`).
- Section-aware chunking that keeps the clause number on every chunk (`chunker.py`). This is what makes
  citations precise and clause-level supersession possible.
- Embeddings (MiniLM and bge, switchable) and the ChromaDB store, one collection per configuration
  (`embeddings.py`, `vector_store.py`).
- The Source Register in CSV and SQLite (`source_register.py`) and `POST /ingest`'s core (`pipeline.py`).
- **The Annex A precedence engine** (`precedence.py`): applicability, supersession, authority, recency,
  unresolved. Used both on retrieved chunks and on rules.
- Collecting the university documents and filling in `docs/source_register.csv`.

**Why it matters for scoring:** grounded answers and citations (20) and versioning, conflicts and live
ingestion (15) both depend on this layer. Judges will ingest unseen documents during live testing.

**Q&A talking points:** why section chunking beat fixed windows (eval numbers); why supersession is checked
against the whole register, not just retrieved chunks; why a level-5 source can never win; how a
batch-scoped circular is handled for a general question.

## Member 2: Data and Tools Engineer

**Owns:** `data/`, `tools/`, `scripts/load_students.py`, `DATA_CARD.md`, `tests/test_tools.py`

**What they build**
- The Annex C schema and DB helpers (`schema.sql`, `db.py`).
- The LLM-based synthetic data generator with verbatim prompts, Pydantic schema enforcement and retries
  (`generate_students.py`, `prompts/`), plus 10 deliberate edge-case students (`edge_cases.py`).
- The validation script and the judge-facing loader (`validate.py`, `loader.py`, `scripts/load_students.py`).
- The rule registry and the rule extractor that turns thresholds in newly ingested documents into rules
  tied to their clause (`rules.py`, `rule_extractor.py`).
- All deterministic tools: attendance, results, profile, exam eligibility, supplementary eligibility,
  placement eligibility with what-if, attendance projection (`student_tools.py`, `eligibility.py`).

**Why it matters for scoring:** tools (15, shared), synthetic data quality (5), and the disqualification
rule that authoritative results must never come from LLM text.

**Q&A talking points:** how a rule gets into the registry and what happens when a new circular changes it;
why code (not the LLM) assigns IDs, totals and backlog counts; which edge case each student ID covers;
what the LLM got wrong during generation and how validation caught it.

## Member 3: Orchestration and Safety Engineer

**Owns:** `graph/`, `shared/llm.py`, `tests/test_graph.py`

**What they build**
- The LangGraph workflow: guard, plan, retrieve, tools, generate, finalize (`workflow.py`, `nodes.py`).
- The LLM client: Ollama with JSON-schema output, validation and retry, cloud fallback switch, mock mode
  (`shared/llm.py`).
- The planner gate: the LLM proposes tools, code validates names, arguments and course codes, and falls
  back to a rule planner (`planner.py`, `courses.py`).
- Safety: identity from the header only, refusal of other students' data, stripping and logging of
  instruction-like text in documents (`safety.py`).
- Answer typing, citation validation, numeric grounding check, and the guarantee that the answer always
  comes from the precedence winner (`nodes.finalize_node`, `templates.py`, `extractive.py`).

**Why it matters for scoring:** orchestration and multi-step (15, shared), safety and responsible AI (10),
architecture judgement (10).

**Q&A talking points:** why one workflow and not multiple agents; why the verdict sentence is templated;
what happens when qwen returns invalid JSON; how "I am S1002" in the message is handled; how the forum
post's injection is neutralised.

## Member 4: Platform, Evaluation and Integration Lead

**Owns:** `api/`, `ui/`, `eval/`, `shared/config.py`, `Dockerfile`, `docker-compose.yml`, `.github/`,
`README.md`, `tests/test_api.py`, `scripts/seed_all.py`, `scripts/export_audit_samples.py`

**What they build**
- The FastAPI app implementing the Section 6 contract exactly, plus the test-student upload endpoint
  (`api/main.py`) and the audit store (`api/audit.py`).
- The Streamlit UI: ask, ingest, sources, audit lookup (`ui/streamlit_app.py`).
- The 25-question evaluation set and the harness computing all six metrics across configurations
  (`eval/`).
- Docker, compose, CI, README, the three sample audit records, and the demo script.

**Why it matters for scoring:** evaluation rigour (10) and engineering quality (10), plus the live-testing
slot only works if the API contract and loader are exact.

**Q&A talking points:** how each metric is computed and its weakness; which configuration won and by how
much; p50/p95 latency and tokens per question; how `docker compose up` connects to Ollama on the host.

## How we merge into one repository

1. **Kickoff contract commit** (hour 1, on `main`): `shared/schemas.py` (all four review it),
   `shared/config.py` (Member 4), `shared/llm.py` (Member 3), `data/schema.sql` + `data/db.py` (Member 2),
   `requirements*.txt`, `.gitignore`.
2. Each member works on `feat/<area>` and commits at least hourly from their own Git identity.
3. Pull requests merge in dependency order: Member 1 (`ingestion/`) → Member 2 (`data/`, `tools/`) →
   Member 3 (`graph/`) → Member 4 (`api/`, `ui/`, `eval/`, Docker). CI is green from step 2 onwards.
4. `CODEOWNERS` requires the owning member's review on each PR.
5. Final: tag `final` on `main` after the eval report is regenerated with Ollama.

```bash
git checkout -b feat/ingestion        # Member 1 (feat/data-tools, feat/orchestration, feat/platform)
git add ingestion/ docs/ tests/test_ingestion.py tests/test_precedence.py
git commit -m "ingestion: section-aware chunking with clause numbers"
git push -u origin feat/ingestion     # then open a PR into main
```

## Demo script (10 minutes)

| Minute | Shown by | Request |
|---|---|---|
| 0-2 | Member 1 | "What is the minimum attendance required?" → 80%, circular cited, step 2 and step 3 shown |
| 2-4 | Member 2 | S1004 "Am I eligible for the exam in Data Structures?" → not eligible, 77.5% vs 80%, 5 classes needed |
| 4-6 | Member 3 | "Scholarship for Antarctica?" → not_found; "attendance of S1002" → refused; hostel curfew → conflict_flagged |
| 6-8 | Member 1 + 2 | Live `/ingest` of a new circular, then the same eligibility question changes answer |
| 8-10 | Member 4 | Eval report: metrics, config comparison, latency; open an audit record |
