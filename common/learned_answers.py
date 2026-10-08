"""
Remembers answers to screening questions you've typed in yourself before,
keyed by a normalized version of the question text, so you're never asked
the same question twice across runs.

Stored as plain JSON in the active user's data directory. This file can
contain personal answers (DOB, etc.) — keep it private like a login session.
"""
import json
import re
from pathlib import Path

from common.data_paths import data_path


def _store_path() -> Path:
    return data_path("learned_answers.json")


def _normalize(question: str) -> str:
    q = question.lower().strip()
    q = re.sub(r"\s+", " ", q)
    q = re.sub(r"[^\w\s]", "", q)  # drop punctuation so minor phrasing differences still match
    return q


def _load() -> dict:
    path = _store_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict):
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_answer(question: str) -> str | None:
    data = _load()
    return data.get(_normalize(question))


def save_answer(question: str, answer: str):
    data = _load()
    data[_normalize(question)] = answer
    _save(data)


def save_unanswered(question: str):
    data = _load()
    key = _normalize(question)
    if key not in data:
        data[key] = ""
        _save(data)
