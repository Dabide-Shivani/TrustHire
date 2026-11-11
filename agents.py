"""The three LLM agents. Each returns strict JSON.
Provider-agnostic: works with any OpenAI-compatible endpoint (Gemini, Groq,
OpenRouter, Ollama, ...). Configure with LLM_API_KEY / LLM_BASE_URL / LLM_MODEL."""
import json
import os
import re
import time

from dotenv import load_dotenv

load_dotenv()
MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash")
BASE_URL = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=os.environ["LLM_API_KEY"], base_url=BASE_URL,
                         timeout=float(os.getenv("LLM_TIMEOUT", "60")), max_retries=0)
    return _client


def _extract_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)     # grab the first {...} block
        if m:
            return json.loads(m.group(0))
        raise


RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


def llm_json(system, user, attempts=4):
    system = system + "\nReturn ONLY valid JSON. No markdown, no commentary."
    last_err = None
    for i in range(attempts):
        try:
            resp = _get_client().chat.completions.create(
                model=MODEL, temperature=0,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}])
            return _extract_json(resp.choices[0].message.content)
        except json.JSONDecodeError as e:
            last_err = e
            print(f"[llm] attempt {i+1}/{attempts}: reply was not valid JSON", flush=True)
            user += "\n\nYour last reply was not valid JSON. Reply with JSON only."
        except Exception as e:
            last_err = e
            code = getattr(e, "status_code", None)
            print(f"[llm] attempt {i+1}/{attempts} failed: {type(e).__name__} "
                  f"{code or ''} {str(e)[:200]}", flush=True)
            if code is not None and code not in RETRYABLE:      # bad key / model name
                raise RuntimeError(f"LLM request rejected (HTTP {code}): {e}") from e
            if i < attempts - 1:
                wait = 10 * (i + 1) if code == 429 else 2 ** (i + 1)
                print(f"[llm] waiting {wait}s before retrying...", flush=True)
                time.sleep(wait)
    raise RuntimeError(f"LLM call failed after {attempts} attempts: {last_err}")


def intake_agent(jd_text):
    return llm_json(
        "You turn a job description into hiring criteria. Output JSON: "
        '{"must_have": ["..."], "nice_to_have": ["..."], "min_experience_years": 0}',
        jd_text)


def screening_agent(criteria, resume_text):
    return llm_json(
        "You screen a resume against hiring criteria. Use ONLY the resume text; "
        "never guess. Judge skills and experience only. Output JSON: "
        '{"score": <0-100 integer>, "matched": [{"skill": "...", '
        '"evidence_quote": "<exact words from the resume>"}], '
        '"gaps": ["..."], "summary": "<2 sentences>"}',
        f"CRITERIA:\n{json.dumps(criteria)}\n\nRESUME:\n{resume_text}")


def consistency_agent(resume_text):
    return llm_json(
        "You check a resume for credibility. Flag skills or achievements that are "
        "claimed but have no supporting project/experience evidence, and any "
        "internal contradictions. Output JSON: "
        '{"flags": [{"claim": "...", "reason": "..."}], "risk": "low|medium|high"}',
        resume_text)
