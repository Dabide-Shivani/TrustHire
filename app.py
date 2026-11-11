"""TrustHire UI.  Run:  streamlit run app.py"""
import json
import os

import pandas as pd
import streamlit as st

from audit import AuditTrail
from main import load_resumes
from pipeline import human_decide, run_pipeline
from store import Store

st.set_page_config(page_title="TrustHire", layout="wide")


@st.cache_resource
def get_store():
    return Store()


store = get_store()

# ---------------------------------------------------------------- sidebar
st.sidebar.title("TrustHire")
st.sidebar.caption("Governed hiring agents: every AI step is explained, "
                   "audited, and a human decides.")
st.sidebar.warning("Use sample or synthetic resumes only. Free API tiers may "
                   "use submitted data to improve their products.")
reviewer = st.sidebar.text_input("Reviewer name", value="recruiter")

jobs = store.list_jobs()
with st.sidebar.expander("New screening run", expanded=not jobs):
    title = st.text_input("Job title", "Junior Backend Engineer")
    default_jd = ""
    if os.path.exists("data/sample_jd.txt"):
        default_jd = open("data/sample_jd.txt", encoding="utf-8").read()
    jd = st.text_area("Job description", default_jd, height=220)
    files = st.file_uploader("Resumes (.txt)", type=["txt"], accept_multiple_files=True)
    st.caption("No files uploaded = the 5 sample resumes are used.")
    if st.button("Run agents", type="primary"):
        resumes = {f.name: f.read().decode("utf-8", "ignore") for f in files} \
            or load_resumes()
        bar = st.progress(0.0, text="Starting...")
        try:
            st.session_state["job_id"] = run_pipeline(
                store, title, jd, resumes,
                progress=lambda msg, frac: bar.progress(frac, text=msg))
            st.rerun()
        except Exception as e:                          # show errors instead of crashing
            st.error(f"Run failed: {e}")
            st.caption("Check the terminal running Streamlit for details.")

jobs = store.list_jobs()
if not jobs:
    st.title("TrustHire")
    st.info("Create a screening run from the sidebar to get started.")
    st.stop()

summaries = store.job_summaries()
by_id = {j["id"]: j for j in summaries}
ids = [j["id"] for j in summaries]


def run_label(i):
    j = by_id[i]
    when = j["created_at"][:16].replace("T", " ") + " UTC"
    state = (f"✓ {j['n_screened']} candidates" if j["complete"]
             else f"⚠ incomplete ({j['n_screened']}/{j['n_candidates']})")
    return f"#{i} · {state} · {when}"


if st.session_state.get("job_id") in ids:
    default_idx = ids.index(st.session_state["job_id"])
else:                                   # default to the newest *complete* run
    default_idx = next((n for n, j in enumerate(summaries) if j["complete"]), 0)
job_id = st.sidebar.selectbox("Screening run", ids, index=default_idx,
                              format_func=run_label)

if not by_id[job_id]["complete"]:
    st.sidebar.warning("This run did not finish.")
    if st.sidebar.button("Delete this incomplete run"):
        store.delete_incomplete_job(job_id)
        st.session_state.pop("job_id", None)
        st.rerun()

st.title(f"Run #{job_id}")
tab_cands, tab_audit = st.tabs(["Candidates", "Audit trail"])

# ---------------------------------------------------------------- candidates
with tab_cands:
    ranked = store.ranked(job_id)
    if not ranked:
        st.warning("No candidates in this run.")
    else:
        st.dataframe(pd.DataFrame([{
            "Candidate": r["external_id"], "Score": r["score"], "Risk": r["risk"],
            "AI suggestion": r["ai_status"], "Human decision": r["human_decision"] or "—",
        } for r in ranked]), hide_index=True)
        st.caption("The AI only suggests. Nothing is final until a human decides.")

        pick = st.selectbox("Review a candidate", [r["external_id"] for r in ranked])
        r = next(x for x in ranked if x["external_id"] == pick)

        left, right = st.columns([2, 1])
        with left:
            st.metric("Score", f"{r['score']}/100")
            st.write(r["summary"])
            st.subheader("Evidence from the resume")
            for m in r["matched"]:
                st.markdown(f"- **{m.get('skill', '')}**: “{m.get('evidence_quote', '')}”")
            if r["gaps"]:
                st.subheader("Gaps")
                for g in r["gaps"]:
                    st.markdown(f"- {g}")
            st.subheader(f"Consistency check: {r['risk']} risk")
            if r["flags"]:
                for f in r["flags"]:
                    st.markdown(f"- **{f.get('claim', '')}**: {f.get('reason', '')}")
            else:
                st.markdown("No flags raised.")
            with st.expander("What the AI actually saw (redacted resume)"):
                st.text(store.get_candidate(job_id, pick)["redacted_text"])

        with right:
            st.subheader("Your decision")
            st.write(f"AI suggestion: **{r['ai_status']}**")
            if r["human_decision"]:
                st.info(f"Current decision: {r['human_decision']}")
            note = st.text_input("Note (optional)", key=f"note_{pick}")
            b1, b2 = st.columns(2)
            if b1.button("Approve", key=f"ap_{pick}"):
                human_decide(store, job_id, pick, "APPROVED", reviewer, note)
                st.rerun()
            if b2.button("Reject", key=f"rj_{pick}"):
                human_decide(store, job_id, pick, "REJECTED", reviewer, note)
                st.rerun()

# ---------------------------------------------------------------- audit
with tab_audit:
    trail = AuditTrail(store, job_id)
    events = [dict(e) for e in store.audit_events(job_id)]
    st.write(f"{len(events)} events. Each stores the hash of the previous one, so "
             "changing any past event breaks the chain.")

    c1, c2 = st.columns(2)
    if c1.button("Verify audit trail"):
        ok, msg = trail.verify()
        if ok:
            st.success(msg)
        else:
            st.error(msg)
    if c2.button("Simulate tampering (on a copy, database untouched)"):
        if len(events) >= 3:
            fake = [dict(e) for e in events]
            fake[2]["output_json"] = json.dumps({"score": 99})
            ok, msg = trail.verify(fake)
            if ok:
                st.success(msg)
            else:
                st.error(f"Tampered copy: {msg}")
        else:
            st.warning("Not enough events to simulate.")

    st.dataframe(pd.DataFrame([{
        "#": e["id"], "Time (UTC)": e["ts"][:19], "Agent": e["agent"],
        "Action": e["action"], "Subject": e["subject"] or "",
        "Hash": e["hash"][:12], "Prev hash": e["prev_hash"][:12],
    } for e in events]), hide_index=True)

    if events:
        eid = st.selectbox("Inspect an event", [e["id"] for e in events])
        ev = next(e for e in events if e["id"] == eid)
        st.json({"agent": ev["agent"], "action": ev["action"], "subject": ev["subject"],
                 "inputs": json.loads(ev["inputs_json"]),
                 "output": json.loads(ev["output_json"])})
