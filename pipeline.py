"""Plain-Python pipeline (LangGraph version comes later).
intake -> [redact -> screen -> consistency -> route] per candidate."""
import agents
from audit import AuditTrail
from fairness import redact_pii


def route(score, risk):
    """AI only *suggests*. A human always makes the final decision."""
    if risk in ("medium", "high") or 50 <= score <= 70:
        return "NEEDS_REVIEW"
    return "SUGGEST_SHORTLIST" if score > 70 else "SUGGEST_REJECT"


def run_pipeline(store, title, jd_text, resumes, progress=None):
    """resumes: {external_id: resume_text}. Returns job_id.
    progress(message, fraction) is called after each step (used by the UI)."""
    job_id = store.create_job(title, jd_text)
    trail = AuditTrail(store, job_id)
    steps_total = 1 + 2 * len(resumes)
    done = 0

    def tick(msg):
        nonlocal done
        done += 1
        if progress:
            progress(msg, min(done / steps_total, 1.0))

    try:
        if progress:
            progress("Reading the job description...", 0.0)
        criteria = agents.intake_agent(jd_text)
        store.set_criteria(job_id, criteria)
        trail.log("intake", "extract_criteria", None, {"jd_chars": len(jd_text)}, criteria)
        tick("Criteria extracted")

        for n, (ext_id, text) in enumerate(resumes.items(), start=1):
            clean = redact_pii(text)
            cand_id = store.add_candidate(job_id, ext_id, text, clean)
            trail.log("fairness_guard", "redact_pii", ext_id,
                      {"chars_before": len(text)}, {"chars_after": len(clean)})

            screen = agents.screening_agent(criteria, clean)
            trail.log("screening", "score", ext_id, {"model_sees": "redacted resume"}, screen)
            tick(f"{ext_id}: scored ({n}/{len(resumes)})")

            check = agents.consistency_agent(clean)
            trail.log("consistency", "flag_claims", ext_id, {}, check)

            status = route(int(screen["score"]), check["risk"])
            store.save_screening(cand_id, screen, check, status)
            trail.log("router", "ai_suggestion", ext_id,
                      {"score": screen["score"], "risk": check["risk"]}, {"status": status})
            tick(f"{ext_id}: checked ({n}/{len(resumes)})")
    except Exception as e:
        trail.log("pipeline", "run_failed", None, {}, {"error": str(e)[:300]})
        raise
    return job_id


def human_decide(store, job_id, ext_id, decision, reviewer, note=""):
    """decision: 'APPROVED' or 'REJECTED'. Logged in the audit trail."""
    cand = store.get_candidate(job_id, ext_id)
    store.record_decision(cand["id"], decision, reviewer, note)
    AuditTrail(store, job_id).log("human", decision.lower(), ext_id,
                                  {"reviewer": reviewer, "note": note}, {"decision": decision})
