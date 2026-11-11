"""Tamper-evident audit trail: each event stores the hash of the previous one,
so editing any past event breaks the chain. Events live in SQLite (store.py)."""
import hashlib
import json

from store import now_iso

GENESIS = "GENESIS"


def _canonical(ts, agent, action, subject, inputs_json, output_json, prev_hash):
    body = {"ts": ts, "agent": agent, "action": action, "subject": subject,
            "inputs": json.loads(inputs_json), "output": json.loads(output_json),
            "prev_hash": prev_hash}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _hash(*parts):
    return hashlib.sha256(_canonical(*parts).encode()).hexdigest()


class AuditTrail:
    def __init__(self, store, job_id):
        self.store = store
        self.job_id = job_id

    def log(self, agent, action, subject, inputs, output):
        prev = self.store.last_audit_hash(self.job_id) or GENESIS
        ts = now_iso()
        inputs_json = json.dumps(inputs, sort_keys=True, default=str)
        output_json = json.dumps(output, sort_keys=True, default=str)
        h = _hash(ts, agent, action, subject, inputs_json, output_json, prev)
        self.store.insert_audit(self.job_id, {
            "ts": ts, "agent": agent, "action": action, "subject": subject,
            "inputs_json": inputs_json, "output_json": output_json,
            "prev_hash": prev, "hash": h})
        return h

    def verify(self, events=None):
        """Returns (ok, message). Replays the whole chain for this job.
        Pass `events` (list of dicts) to check a copy without touching the DB."""
        prev = GENESIS
        if events is None:
            events = self.store.audit_events(self.job_id)
        for e in events:
            expected = _hash(e["ts"], e["agent"], e["action"], e["subject"],
                             e["inputs_json"], e["output_json"], e["prev_hash"])
            if e["prev_hash"] != prev:
                return False, f"Chain broken at event #{e['id']} (prev_hash mismatch)"
            if e["hash"] != expected:
                return False, f"Event #{e['id']} was modified (hash mismatch)"
            prev = e["hash"]
        return True, "Audit trail intact"
