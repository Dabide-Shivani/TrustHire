"""Fairness guard: strip personal identifiers BEFORE any AI agent sees a resume."""
import re

_LABELLED = re.compile(
    r"(?im)^\s*(gender|sex|age|dob|date of birth|marital status|religion|"
    r"nationality|photo|father'?s name)\b.*$")


def redact_pii(text):
    lines = text.strip().splitlines()
    if lines:                       # heuristic: first line of a resume is the name
        lines[0] = "[CANDIDATE]"
    text = "\n".join(lines)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[EMAIL]", text)
    text = re.sub(r"\+?\d[\d\s().-]{8,}\d", "[PHONE]", text)
    text = re.sub(r"(?i)(https?://)?(www\.)?linkedin\.com/\S+", "[LINKEDIN]", text)
    text = _LABELLED.sub("[REMOVED]", text)
    return text
