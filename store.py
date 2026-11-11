"""SQLite persistence for TrustHire: jobs, candidates, screenings,
human decisions and the hash-chained audit log."""
import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("TRUSTHIRE_DB", "trusthire.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    jd_text       TEXT NOT NULL,
    criteria_json TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL REFERENCES jobs(id),
    external_id   TEXT NOT NULL,          -- e.g. file name / application id
    resume_text   TEXT NOT NULL,          -- original (kept for the recruiter only)
    redacted_text TEXT,                   -- what the AI actually sees
    created_at    TEXT NOT NULL,
    UNIQUE(job_id, external_id)
);

CREATE TABLE IF NOT EXISTS screenings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id  INTEGER NOT NULL UNIQUE REFERENCES candidates(id),
    score         INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    matched_json  TEXT NOT NULL,          -- [{skill, evidence_quote}]
    gaps_json     TEXT NOT NULL,
    summary       TEXT,
    risk          TEXT NOT NULL CHECK (risk IN ('low','medium','high')),
    flags_json    TEXT NOT NULL,          -- consistency-agent flags
    ai_status     TEXT NOT NULL CHECK (ai_status IN
                  ('SUGGEST_SHORTLIST','SUGGEST_REJECT','NEEDS_REVIEW')),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (   -- the human always has the final word
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id  INTEGER NOT NULL REFERENCES candidates(id),
    decision      TEXT NOT NULL CHECK (decision IN ('APPROVED','REJECTED')),
    reviewer      TEXT NOT NULL,
    note          TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL REFERENCES jobs(id),
    ts            TEXT NOT NULL,
    agent         TEXT NOT NULL,
    action        TEXT NOT NULL,
    subject       TEXT,                   -- candidate external_id, or NULL
    inputs_json   TEXT NOT NULL,
    output_json   TEXT NOT NULL,
    prev_hash     TEXT NOT NULL,
    hash          TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_cand_job   ON candidates(job_id);
CREATE INDEX IF NOT EXISTS idx_audit_job  ON audit_events(job_id, id);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    def __init__(self, path=None):
        self.path = path or DB_PATH
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- jobs ----------
    def create_job(self, title, jd_text):
        cur = self.conn.execute(
            "INSERT INTO jobs(title, jd_text, created_at) VALUES (?,?,?)",
            (title, jd_text, now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def set_criteria(self, job_id, criteria):
        self.conn.execute("UPDATE jobs SET criteria_json=? WHERE id=?",
                          (json.dumps(criteria), job_id))
        self.conn.commit()

    def get_job(self, job_id):
        return self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def job_summaries(self):
        """Runs with progress counts, newest first. complete = every candidate screened."""
        rows = self.conn.execute(
            """SELECT j.id, j.title, j.created_at,
                      (SELECT COUNT(*) FROM candidates c WHERE c.job_id=j.id) AS n_candidates,
                      (SELECT COUNT(*) FROM candidates c JOIN screenings s
                       ON s.candidate_id=c.id WHERE c.job_id=j.id) AS n_screened
               FROM jobs j ORDER BY j.id DESC""").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["complete"] = d["n_candidates"] > 0 and d["n_candidates"] == d["n_screened"]
            out.append(d)
        return out

    def delete_incomplete_job(self, job_id):
        """Remove a failed/interrupted run. Completed runs can never be deleted
        (their audit trail must stay)."""
        summary = next((j for j in self.job_summaries() if j["id"] == job_id), None)
        if summary is None:
            raise ValueError("No such run")
        if summary["complete"]:
            raise ValueError("Completed runs cannot be deleted")
        c = self.conn
        ids = "(SELECT id FROM candidates WHERE job_id=?)"
        c.execute(f"DELETE FROM decisions WHERE candidate_id IN {ids}", (job_id,))
        c.execute(f"DELETE FROM screenings WHERE candidate_id IN {ids}", (job_id,))
        c.execute("DELETE FROM candidates WHERE job_id=?", (job_id,))
        c.execute("DELETE FROM audit_events WHERE job_id=?", (job_id,))
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        c.commit()

    def list_jobs(self):
        return self.conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()

    # ---------- candidates ----------
    def add_candidate(self, job_id, external_id, resume_text, redacted_text):
        cur = self.conn.execute(
            """INSERT INTO candidates(job_id, external_id, resume_text,
               redacted_text, created_at) VALUES (?,?,?,?,?)""",
            (job_id, external_id, resume_text, redacted_text, now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def get_candidate(self, job_id, external_id):
        return self.conn.execute(
            "SELECT * FROM candidates WHERE job_id=? AND external_id=?",
            (job_id, external_id)).fetchone()

    # ---------- screenings ----------
    def save_screening(self, candidate_id, screen, check, ai_status):
        self.conn.execute(
            """INSERT OR REPLACE INTO screenings(candidate_id, score, matched_json,
               gaps_json, summary, risk, flags_json, ai_status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (candidate_id, int(screen["score"]),
             json.dumps(screen.get("matched", [])),
             json.dumps(screen.get("gaps", [])),
             screen.get("summary", ""), check["risk"],
             json.dumps(check.get("flags", [])), ai_status, now_iso()))
        self.conn.commit()

    def ranked(self, job_id):
        """Candidates with AI results and latest human decision, best score first."""
        rows = self.conn.execute(
            """SELECT c.id AS candidate_id, c.external_id, s.score, s.risk,
                      s.ai_status, s.summary, s.matched_json, s.gaps_json, s.flags_json,
                      (SELECT d.decision FROM decisions d WHERE d.candidate_id=c.id
                       ORDER BY d.id DESC LIMIT 1) AS human_decision
               FROM candidates c JOIN screenings s ON s.candidate_id=c.id
               WHERE c.job_id=? ORDER BY s.score DESC""", (job_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("matched_json", "gaps_json", "flags_json"):
                d[k[:-5]] = json.loads(d.pop(k))
            out.append(d)
        return out

    # ---------- human decisions ----------
    def record_decision(self, candidate_id, decision, reviewer, note=""):
        self.conn.execute(
            """INSERT INTO decisions(candidate_id, decision, reviewer, note, created_at)
               VALUES (?,?,?,?,?)""",
            (candidate_id, decision, reviewer, note, now_iso()))
        self.conn.commit()

    # ---------- audit events (used by audit.py) ----------
    def last_audit_hash(self, job_id):
        row = self.conn.execute(
            "SELECT hash FROM audit_events WHERE job_id=? ORDER BY id DESC LIMIT 1",
            (job_id,)).fetchone()
        return row["hash"] if row else None

    def insert_audit(self, job_id, e):
        self.conn.execute(
            """INSERT INTO audit_events(job_id, ts, agent, action, subject,
               inputs_json, output_json, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)""",
            (job_id, e["ts"], e["agent"], e["action"], e["subject"],
             e["inputs_json"], e["output_json"], e["prev_hash"], e["hash"]))
        self.conn.commit()

    def audit_events(self, job_id):
        return self.conn.execute(
            "SELECT * FROM audit_events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()

    def close(self):
        self.conn.close()
