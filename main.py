"""CLI runner: python main.py  (needs LLM_API_KEY in .env)"""
import glob
import os

from audit import AuditTrail
from pipeline import run_pipeline
from store import Store


def load_resumes(folder="data/resumes"):
    out = {}
    for path in sorted(glob.glob(os.path.join(folder, "*.txt"))):
        with open(path, encoding="utf-8") as f:
            out[os.path.basename(path)] = f.read()
    return out


if __name__ == "__main__":
    store = Store()
    jd = open("data/sample_jd.txt", encoding="utf-8").read()
    job_id = run_pipeline(store, "Junior Backend Engineer", jd, load_resumes())

    print(f"\nJob #{job_id} results\n" + "-" * 62)
    for r in store.ranked(job_id):
        print(f"{r['external_id']:<22} score={r['score']:>3}  risk={r['risk']:<6} {r['ai_status']}")
    ok, msg = AuditTrail(store, job_id).verify()
    print("-" * 62 + f"\nAudit check: {msg}")
