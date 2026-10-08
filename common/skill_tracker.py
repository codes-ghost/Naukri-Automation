"""
Persistent frequency counter for the job skills that keep causing a
"Key Skills" match-cross on Naukri job detail pages.

Every time a job's Key Skills row comes back as a mismatch, the preferred
keyskills advertised on that job (the chips flagged with Naukri's
`ni-icon-jd-save` icon) are tallied here. Over many runs this builds a ranked
picture of which skills to add to your profile to convert more matches.

Stored as plain JSON in the active user's data directory, keyed by the skill
name (as Naukri spells it) -> how many mismatched jobs needed it, e.g.:

    {
        "TypeScript": 4,
        "Playwright Automation": 7,
        "Automation Testing": 3
    }
"""
import json
import re
from pathlib import Path

from common.data_paths import data_path


def _normalize(skill: str) -> str:
    return re.sub(r"\s+", " ", (skill or "").strip())


def load_counts(path: str | Path | None = None) -> dict[str, int]:
    target = Path(path) if path else data_path("missing_skills.json")
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(skill): int(count) for skill, count in data.items() if isinstance(count, int)}


def save_counts(counts: dict[str, int], path: str | Path | None = None):
    target = Path(path) if path else data_path("missing_skills.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    ranked = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].casefold())))
    target.write_text(json.dumps(ranked, indent=2, ensure_ascii=False), encoding="utf-8")


def record_missing(skills, path: str | Path | None = None) -> dict[str, int]:
    """Increments the counter for each skill name and returns the whole map.

    Names are de-duplicated per call so a skill listed twice on the same job
    still only counts once for that job.
    """
    counts = load_counts(path)
    seen = set()
    for skill in skills or []:
        name = _normalize(skill)
        if not name or name in seen:
            continue
        seen.add(name)
        counts[name] = counts.get(name, 0) + 1
    if seen:
        save_counts(counts, path)
    return counts
