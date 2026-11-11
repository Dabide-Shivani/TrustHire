"""Run: python -m unittest tests.test_core -v   (no API key needed: agents are mocked)"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents
from audit import AuditTrail
from fairness import redact_pii
from pipeline import human_decide, route, run_pipeline
from store import Store

FAKE_CRITERIA = {"must_have": ["Java", "SQL"], "nice_to_have": ["Docker"], "min_experience_years": 0}


def fake_screen(criteria, resume):
    assert "@" not in resume, "AI must never see an email address"
    return {"score": 85 if "Spring" in resume else 40,
            "matched": [{"skill": "Java", "evidence_quote": "Java"}],
            "gaps": [], "summary": "ok"}


def fake_check(resume):
    return {"flags": [], "risk": "high" if "expert" in resume else "low"}


class CoreTests(unittest.TestCase):
    def setUp(self):
        # TemporaryDirectory cleans up safely on Windows (no open file handle)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmpdir.name, "test.db"))

    def tearDown(self):
        self.store.close()
        self.tmpdir.cleanup()

    def _run(self):
        resumes = {
            "a.txt": "Aarav\na@x.com\nGender: Male\nSpring Boot Java SQL",
            "b.txt": "Bina\nb@x.com\nHTML CSS",
            "c.txt": "Chet\nc@x.com\nJava expert Spring",
        }
        with mock.patch.object(agents, "intake_agent", return_value=FAKE_CRITERIA), \
             mock.patch.object(agents, "screening_agent", side_effect=fake_screen), \
             mock.patch.object(agents, "consistency_agent", side_effect=fake_check):
            return run_pipeline(self.store, "Test job", "jd", resumes)

    def test_redaction(self):
        out = redact_pii("Name X\nx@y.com | +91 98765 43210\nGender: Male\nJava dev")
        self.assertNotIn("x@y.com", out)
        self.assertNotIn("98765", out)
        self.assertNotIn("Male", out)
        self.assertNotIn("Name X", out)
        self.assertIn("Java dev", out)

    def test_routing(self):
        self.assertEqual(route(85, "low"), "SUGGEST_SHORTLIST")
        self.assertEqual(route(30, "low"), "SUGGEST_REJECT")
        self.assertEqual(route(60, "low"), "NEEDS_REVIEW")
        self.assertEqual(route(90, "high"), "NEEDS_REVIEW")

    def test_pipeline_and_ranking(self):
        job_id = self._run()
        ranked = self.store.ranked(job_id)
        self.assertEqual([r["external_id"] for r in ranked], ["a.txt", "c.txt", "b.txt"])
        self.assertEqual(ranked[1]["ai_status"], "NEEDS_REVIEW")   # high risk
        ok, _ = AuditTrail(self.store, job_id).verify()
        self.assertTrue(ok)

    def test_human_decision_logged(self):
        job_id = self._run()
        human_decide(self.store, job_id, "a.txt", "APPROVED", "recruiter1", "good fit")
        self.assertEqual(self.store.ranked(job_id)[0]["human_decision"], "APPROVED")
        self.assertTrue(AuditTrail(self.store, job_id).verify()[0])

    def test_tampering_is_detected(self):
        job_id = self._run()
        self.store.conn.execute(
            "UPDATE audit_events SET output_json='{\"score\": 99}' WHERE id=3")
        self.store.conn.commit()
        ok, msg = AuditTrail(self.store, job_id).verify()
        self.assertFalse(ok)
        print("  tamper message ->", msg)

    def test_deleting_an_event_is_detected(self):
        job_id = self._run()
        self.store.conn.execute("DELETE FROM audit_events WHERE id=4")
        self.store.conn.commit()
        self.assertFalse(AuditTrail(self.store, job_id).verify()[0])


class RunListTests(unittest.TestCase):
    def test_summaries_and_delete_guard(self):
        tmp = tempfile.TemporaryDirectory()
        store = Store(os.path.join(tmp.name, "r.db"))
        # one complete run
        with mock.patch.object(agents, "intake_agent", return_value=FAKE_CRITERIA), \
             mock.patch.object(agents, "screening_agent", side_effect=fake_screen), \
             mock.patch.object(agents, "consistency_agent", side_effect=fake_check):
            done = run_pipeline(store, "ok", "jd", {"a.txt": "N\nJava Spring"})
        # one failed run
        with mock.patch.object(agents, "intake_agent", return_value=FAKE_CRITERIA), \
             mock.patch.object(agents, "screening_agent", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                run_pipeline(store, "bad", "jd", {"a.txt": "N\nJava"})
        s = {j["title"]: j for j in store.job_summaries()}
        self.assertTrue(s["ok"]["complete"])
        self.assertFalse(s["bad"]["complete"])
        with self.assertRaises(ValueError):
            store.delete_incomplete_job(done)                 # completed: protected
        store.delete_incomplete_job(s["bad"]["id"])
        self.assertEqual([j["title"] for j in store.job_summaries()], ["ok"])
        self.assertTrue(AuditTrail(store, done).verify()[0])  # survivor untouched
        store.close()
        tmp.cleanup()


class FakeHTTPError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.status_code = code


class RetryTests(unittest.TestCase):
    def _fake_client(self, behaviours):
        calls = iter(behaviours)

        def create(**kw):
            b = next(calls)
            if isinstance(b, Exception):
                raise b
            msg = mock.Mock()
            msg.message.content = b
            return mock.Mock(choices=[msg])
        client = mock.Mock()
        client.chat.completions.create.side_effect = create
        return client

    def test_retries_on_rate_limit_then_succeeds(self):
        client = self._fake_client([FakeHTTPError(429), '{"ok": 1}'])
        with mock.patch.object(agents, "_get_client", return_value=client), \
             mock.patch.object(agents.time, "sleep") as sl:
            self.assertEqual(agents.llm_json("s", "u"), {"ok": 1})
            sl.assert_called_once_with(10)

    def test_bad_key_fails_fast(self):
        client = self._fake_client([FakeHTTPError(401)])
        with mock.patch.object(agents, "_get_client", return_value=client), \
             mock.patch.object(agents.time, "sleep") as sl:
            with self.assertRaises(RuntimeError):
                agents.llm_json("s", "u")
            sl.assert_not_called()

    def test_progress_callback_and_failure_audit(self):
        tmp = tempfile.TemporaryDirectory()
        store = Store(os.path.join(tmp.name, "p.db"))
        seen = []
        with mock.patch.object(agents, "intake_agent", return_value=FAKE_CRITERIA), \
             mock.patch.object(agents, "screening_agent", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                run_pipeline(store, "t", "jd", {"a.txt": "N\nJava"},
                             progress=lambda m, f: seen.append(f))
        job_id = store.list_jobs()[0]["id"]
        last = store.audit_events(job_id)[-1]
        self.assertEqual(last["action"], "run_failed")
        self.assertTrue(AuditTrail(store, job_id).verify()[0])
        self.assertTrue(seen)
        store.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
