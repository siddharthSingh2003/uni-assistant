"""Streamlit UI (M4). Minimal by design: the guide scores the UI only for usability."""
from __future__ import annotations

import os
import pandas as pd
import requests
import streamlit as st

# 127.0.0.1, not localhost: on Windows each localhost request first waits ~2s on IPv6
API = os.getenv("API_URL", "http://127.0.0.1:8000")
MAX_PREVIEW_PAGES = 30
TYPE_LABEL = {"retrieved_fact": ("Retrieved fact", "blue"), "calculated": ("Calculated by tool", "green"),
              "not_found": ("Not found", "gray"), "clarification_needed": ("Needs clarification", "orange"),
              "refused": ("Refused", "red"), "conflict_flagged": ("Conflict flagged", "violet")}

st.set_page_config(page_title="Student Services Assistant", page_icon="🎓", layout="wide")


@st.cache_resource
def _http() -> requests.Session:
    return requests.Session()   # keep-alive: one connection reused across reruns


HTTP = _http()


def logout(message: str = "") -> None:
    st.session_state.clear()
    st.session_state.flash = message
    st.rerun()


def api(method: str, path: str, **kw) -> requests.Response:
    """Authenticated API call. An expired or rejected token sends the user back to the login page."""
    r = HTTP.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {st.session_state.token}"}, **kw)
    if r.status_code == 401:
        logout("Your session has expired. Please log in again.")
    return r


# Streamlit re-runs this whole script on every interaction, so anything that does not
# change between questions is cached instead of being fetched again each time.
@st.cache_data(ttl=30, show_spinner=False)
def health() -> dict:
    return HTTP.get(f"{API}/health", timeout=5).json()


@st.cache_data(ttl=300, show_spinner=False)
def sources() -> list:
    return HTTP.get(f"{API}/sources", timeout=10).json()


@st.cache_data(ttl=600, max_entries=16, show_spinner=False)
def document_bytes(doc_id: str, _headers: dict) -> bytes:
    """The stored original of an ingested document (the same for every logged-in user)."""
    r = HTTP.get(f"{API}/documents/{doc_id}/file", headers=_headers, timeout=60)
    r.raise_for_status()
    return r.content


@st.cache_data(max_entries=8, show_spinner=False)
def pdf_pages(data: bytes) -> list[bytes]:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [page.get_pixmap(dpi=110).tobytes("png") for page in list(doc)[:MAX_PREVIEW_PAGES]]


def show_file(name: str, data: bytes) -> None:
    """View a document in the page: PDF pages as images (works for scans too), images, or text."""
    suffix = os.path.splitext(name)[1].lower()
    if suffix == ".pdf":
        pages = pdf_pages(data)
        st.caption(f"{name} - showing {len(pages)} page(s)")
        for png in pages:
            st.image(png, width="stretch")
    elif suffix in (".png", ".jpg", ".jpeg"):
        st.image(data, caption=name)
    else:
        st.text(data.decode("utf-8", errors="replace"))


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {st.session_state.token}"}


def render_answer(resp: dict) -> None:
    label, colour = TYPE_LABEL.get(resp["answer_type"], (resp["answer_type"], "gray"))
    st.markdown(f":{colour}-background[{label}]  trace `{resp['trace_id']}`")
    st.write(resp["answer"])
    if resp.get("explanation"):
        st.caption(resp["explanation"])
    for a in resp.get("assumptions", []):
        st.info(f"Assumption: {a}")
    for u in resp.get("upcoming_changes", []):
        st.warning(f"Upcoming change: {u}")
    if resp["citations"]:
        with st.expander(f"Citations ({len(resp['citations'])})"):
            st.dataframe(pd.DataFrame(resp["citations"]), hide_index=True)
    if resp["tools_invoked"]:
        with st.expander(f"Tools invoked ({len(resp['tools_invoked'])})"):
            st.json(resp["tools_invoked"])
    if resp["conflicts_detected"]:
        with st.expander(f"Conflicts resolved ({len(resp['conflicts_detected'])})"):
            st.json(resp["conflicts_detected"])


# ---- login gate: nothing below this block runs without a token ----
if "token" not in st.session_state:
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.title("🎓 Student Services Assistant")
        flash = st.session_state.pop("flash", "")
        if flash:
            st.warning(flash)
        with st.form("login"):
            sid = st.text_input("Student ID or admin username", placeholder="e.g. S1001")
            pwd = st.text_input("Password", type="password")
            if st.form_submit_button("Log in", type="primary", width="stretch"):
                try:
                    r = HTTP.post(f"{API}/auth/login", json={"student_id": sid.strip(), "password": pwd}, timeout=15)
                except requests.RequestException:
                    st.error(f"API not reachable at {API}")
                else:
                    if r.ok:
                        st.session_state.token = r.json()["access_token"]
                        st.session_state.user = {k: r.json()[k] for k in ("role", "name", "student")}
                        st.rerun()
                    st.error("Incorrect ID or password." if r.status_code in (401, 422) else r.text)
    st.stop()

user = st.session_state.user
is_admin, student = user["role"] == "admin", user["student"]
with st.sidebar:
    st.header(user["name"])
    st.caption("Administrator" if is_admin else
               f"{student['student_id']} · {student['programme']} · batch {student['batch_year']}")
    if st.button("Log out", width="stretch"):
        logout()
    try:
        h = health()
        st.caption(f"API {h['status']} · {h['vector_store'].get('chunks', '?')} chunks · "
                   f"LLM {h['llm'].get('status')}")
    except Exception:  # noqa: BLE001
        st.error(f"API not reachable at {API}")

if is_admin:      # only an admin reviews student uploads and reads audit records; the API enforces it too
    ask_tab, ingest_tab, review_tab, sources_tab, audit_tab = st.tabs(
        ["Ask", "Add a document", "Review uploads", "Documents", "Audit"])
else:
    ask_tab, ingest_tab, sources_tab = st.tabs(["Ask", "Add a document", "Documents"])
    review_tab = audit_tab = None

with ask_tab:
    st.title("Ask about university rules" if is_admin else "Ask about university rules or your records")
    if "history" not in st.session_state:
        st.session_state.history = []
    earlier = list(st.session_state.history)
    q = st.chat_input("e.g. What is the minimum attendance for end-semester exams?" if is_admin else
                      "e.g. Am I eligible for the supplementary exam in Mathematics?")
    if q:
        # show the question straight away and keep a visible "working" state while the model answers
        with st.chat_message("user"):
            st.write(q)
        with st.chat_message("assistant"):
            try:
                with st.spinner("Checking the rules and your records..."):
                    r = api("post", "/ask", json={"question": q}, timeout=300)   # answered as of today
                    r.raise_for_status()
                render_answer(r.json())
                st.session_state.history.append((q, r.json()))
            except requests.RequestException as e:
                st.error(f"Request failed: {e}")
    for question, resp in reversed(earlier):
        with st.chat_message("user"):
            st.write(question)
        with st.chat_message("assistant"):
            render_answer(resp)

LEVELS = {1: "1 - Regulation / ordinance", 2: "2 - Circular / office order", 3: "3 - Departmental notice",
          4: "4 - FAQ / help desk", 5: "5 - Unofficial (informational only)"}
SOURCE_LABEL = {"llm": "read by the model, checked against the text", "pattern": "found by pattern",
                "default": "not stated in the document - default used", "admin": "set by you"}

with ingest_tab:
    st.subheader("Add a document while the system is running")
    if is_admin:
        st.caption("Choose the authority level. The title, issuer, dates, scope and what the document supersedes "
                   "are read from the document itself.")
    else:
        st.caption("Your document is sent to an admin for review. Nothing is searchable until it is approved; "
                   "approved student documents are stored as unofficial (authority level 5), shown for "
                   "information only, and never override an official source.")
    with st.form("ingest", clear_on_submit=True):
        up = st.file_uploader("Document", type=["pdf", "txt", "md", "png", "jpg"])
        # the API ignores the level for a student whatever is sent; only an admin gets the choice
        level = st.selectbox("Authority level (Annex A)", list(LEVELS), index=1, format_func=LEVELS.get) \
            if is_admin else 5
        submitted = st.form_submit_button("Add document" if is_admin else "Send for review", type="primary")
    if submitted:
        if not up:
            st.error("Choose a file first.")
        else:
            with st.spinner("Reading the document and extracting its metadata..." if is_admin else "Uploading..."):
                r = api("post", "/ingest", files={"file": (up.name, up.getvalue())},
                        data={"authority_level": str(level)}, timeout=600)
            if not r.ok:
                st.error(r.json().get("detail", r.text))
            elif r.json()["status"] == "pending_review":
                st.success(f"{up.name} was sent to an admin for review. You can follow its status below.")
            else:
                res = r.json()
                sources.clear()
                st.success(f"Added {res['doc_id']}: {res['chunks_indexed']} chunks indexed, "
                           f"{res['rules_extracted']} rule(s) extracted. It is searchable now.")
                how = res.get("extracted_by") or {}
                st.caption("Metadata stored in the Source Register - please check it:")
                st.dataframe(pd.DataFrame([{"field": k, "value": v, "source": SOURCE_LABEL.get(how.get(k), how.get(k, ""))}
                                           for k, v in res["metadata"].items()]), hide_index=True)
    if not is_admin:
        mine = api("get", "/uploads/mine", timeout=10).json()
        if mine:
            st.subheader("My submissions")
            st.dataframe(pd.DataFrame([{"file": u["filename"], "status": u["status"], "submitted": u["submitted_at"],
                                        "reviewed": u["reviewed_at"] or "", "admin note": u["note"] or "",
                                        "document id": u["doc_id"] or ""} for u in mine]), hide_index=True)

if review_tab is not None:
    with review_tab:
        st.subheader("Student uploads waiting for review")
        if note := st.session_state.pop("review_flash", ""):
            st.success(note)
        queue = api("get", "/admin/uploads", timeout=10).json()
        if not queue:
            st.info("Nothing is waiting for review.")
        for u in queue:
            uid = u["upload_id"]
            with st.expander(f"{u['filename']}  -  from {u['student_id']}  -  {u['submitted_at']}", expanded=len(queue) == 1):
                st.caption(f"{u['size'] / 1024:.0f} KB. Read the document before deciding: once approved it is "
                           "searchable by every user, as an unofficial (level 5) source.")
                # not cached: a pending file is for admins only
                data = api("get", f"/admin/uploads/{uid}/file", timeout=60).content
                st.download_button("Download", data, file_name=u["filename"], mime=u["content_type"], key=f"dl-{uid}")
                if st.toggle("View document", key=f"view-{uid}"):
                    show_file(u["filename"], data)
                reason = st.text_input("Note to the student (optional)", key=f"note-{uid}")
                c1, c2, _ = st.columns([1, 1, 4])
                if c1.button("Approve", type="primary", key=f"ok-{uid}"):
                    with st.spinner("Indexing the document..."):
                        r = api("post", f"/admin/uploads/{uid}/approve", json={"note": reason}, timeout=600)
                    if r.ok:
                        sources.clear()
                        st.session_state.review_flash = f"Approved {u['filename']} as {r.json()['doc_id']}."
                        st.rerun()
                    st.error(r.json().get("detail", r.text))
                if c2.button("Reject", key=f"no-{uid}"):
                    r = api("post", f"/admin/uploads/{uid}/reject", json={"note": reason}, timeout=30)
                    if r.ok:
                        st.session_state.review_flash = f"Rejected {u['filename']}."
                        st.rerun()
                    st.error(r.json().get("detail", r.text))

with sources_tab:
    st.subheader("Documents")
    try:
        docs = sources()
    except Exception as e:  # noqa: BLE001
        docs = []
        st.error(str(e))
    if docs:
        st.dataframe(pd.DataFrame(docs).drop(columns=["file_path", "stored_file", "stored_size"], errors="ignore"),
                     hide_index=True)
        with_file = {d["doc_id"]: d for d in docs if d.get("stored_file")}
        chosen = st.selectbox("Open a document", list(with_file), index=None, placeholder="Choose a document",
                              format_func=lambda i: f"{i} - {with_file[i]['title']}")
        if chosen:
            d = with_file[chosen]
            try:
                data = document_bytes(chosen, auth_headers())
            except requests.RequestException as e:
                st.error(f"Could not load the file: {e}")
            else:
                st.download_button(f"Download {d['stored_file']} ({d['stored_size'] / 1024:.0f} KB)", data,
                                   file_name=d["stored_file"])
                show_file(d["stored_file"], data)

if audit_tab is not None:
    with audit_tab:
        st.subheader("Look up an audit record")
        tid = st.text_input("trace_id")
        if tid:
            r = api("get", f"/audit/{tid.strip()}", timeout=10)
            st.json(r.json()) if r.ok else st.error("No audit record with that trace_id.")
