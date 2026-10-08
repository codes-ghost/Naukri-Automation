"""
Naukri auto-apply. Requires session_naukri.json from login_capture.py.

Usage:
    python naukri_apply.py

Stops the ENTIRE run immediately and reports if it hits a CAPTCHA, a login
prompt, or a rate-limit warning. Never attempts to solve any of those --
that's the point. Everything else (a bad selector, a question it can't
answer, a stray page error) is handled per-listing: that one listing is
skipped and logged, and the run continues.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from common import learned_answers
from common import skill_tracker
from common.data_paths import data_path

SESSION_FILE = data_path("session_naukri.json")
LOG_FILE = data_path("applications_log.csv")
RECOMMENDED_JOBS_URL = "https://www.naukri.com/mnjuser/recommendedjobs"
RECOMMENDED_CATEGORIES = {
    "Applies": ("applies", "apply", "applied"),
    "Profile": ("profile",),
    "Top Candidate": ("top candidate", "top candidates"),
    "Preference": ("preference", "preferences"),
    "You Might Like": ("you might like", "might like"),
}
RECRUITER_EMAILS_FILE = data_path("recruiter_emails.csv")
EXTERNAL_APPLIES_FILE = data_path("external_apply_links.csv")
VISITED_JOBS_FILE = data_path("visited_recommended_jobs.json")
REJECTED_JOBS_FILE = data_path("rejected_recommended_jobs.csv")
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
DEFAULT_MAX_SCROLLS = 5
DEFAULT_ACTION_DELAY_SECONDS = 5

STOP_PHRASES = [
    "too many requests", "unusual activity", "verify you are human",
    "captcha", "temporarily blocked", "please try again later and reduce",
    "there was an error while processing your request",
]

# The four "job match" rows Naukri renders on a job detail page, each a
# `.styles_MS__details__iS7mj` element carrying an icon (PASS) or cross (FAIL).
# They are classified by their visible label text -- never by position -- because
# Naukri can reorder or add rows. A job must PASS all four required match rows
# to be eligible; Key Skills failures are also tracked for profile optimisation.
MATCH_PASS = "PASS"
MATCH_FAIL = "FAIL"
MATCH_UNKNOWN = "UNKNOWN"
REQUIRED_MATCH_LABELS = ["Early Applicant", "Location", "Work Experience", "Key Skills"]


def log_row(row: list):
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    new_file = not Path(LOG_FILE).exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "source", "title", "company", "status", "reason"])
        w.writerow(row)


def append_csv_row(file_path: str, headers: list[str], row: list):
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(headers)
        writer.writerow(row)


def save_session_state(context, session_file: str | Path = SESSION_FILE):
    try:
        context.storage_state(path=str(session_file))
    except Exception as exc:
        print(f"  (couldn't refresh saved Naukri session: {exc})")


def load_visited_job_ids(file_path: str | Path = VISITED_JOBS_FILE) -> set[str]:
    path = Path(file_path)
    if not path.exists():
        return set()
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        return {str(value) for value in values}
    except (json.JSONDecodeError, OSError, TypeError):
        return set()


def save_visited_job_ids(job_ids: set[str], file_path: str | Path = VISITED_JOBS_FILE):
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(job_ids), indent=2), encoding="utf-8")


def load_rejected_job_ids(file_path: str | Path = REJECTED_JOBS_FILE) -> set[str]:
    path = Path(file_path)
    if not path.exists():
        return set()
    try:
        with path.open("r", newline="", encoding="utf-8") as source:
            return {row.get("job_id", "") for row in csv.DictReader(source) if row.get("job_id")}
    except (OSError, csv.Error):
        return set()


def is_green_tick_rejection(reason: str) -> bool:
    return (reason or "").startswith("green-tick mismatch:")


def is_explicit_detail_rejection(reason: str) -> bool:
    reason = reason or ""
    return (
        is_green_tick_rejection(reason)
        or reason.startswith("Key Skills did not show a green check")
        or reason.startswith("job was posted ")
    )


def record_rejected_job(job_id: str, category: str, reason: str,
                        file_path: str | Path = REJECTED_JOBS_FILE):
    if not job_id:
        return
    path = Path(file_path)
    if job_id in load_rejected_job_ids(path):
        return
    append_csv_row(
        str(path),
        ["job_id", "category", "reason", "timestamp"],
        [job_id, category, reason, datetime.now().isoformat(timespec="seconds")],
    )


def rejected_cards(cards: list[dict], rejected_ids: set[str]) -> list[dict]:
    return [card for card in cards if str(card.get("jobId") or "") in rejected_ids]


def normalize_job_key(title: str, company: str) -> tuple[str, str]:
    return (
        " ".join((title or "").casefold().split()),
        " ".join((company or "").casefold().split()),
    )


def load_previously_handled_job_keys(file_path: str | Path = LOG_FILE) -> set[tuple[str, str]]:
    path = Path(file_path)
    if not path.exists():
        return set()
    handled = set()
    try:
        with path.open("r", newline="", encoding="cp1252", errors="replace") as source:
            for row in csv.DictReader(source):
                if (row.get("source", "").casefold() == "naukri"
                        and row.get("status", "").casefold() in {"applied", "uncertain"}):
                    key = normalize_job_key(row.get("title", ""), row.get("company", ""))
                    if all(key):
                        handled.add(key)
    except (OSError, csv.Error):
        return set()
    return handled


def is_already_applied(page) -> bool:
    return bool(safe_evaluate(page, """
        () => {
            const button = document.querySelector('#apply-button');
            const label = (button?.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            if (label && label !== 'apply' && /applied|submitted/.test(label)) return true;
            const body = (document.body?.innerText || '').toLowerCase();
            return /you have already applied|application already submitted|already applied to this job/.test(body);
        }
    """, default=False))


def log_recruiter_email(category: str, card: dict, email: str):
    append_csv_row(
        RECRUITER_EMAILS_FILE,
        ["timestamp", "category", "title", "company", "email", "job_url"],
        [datetime.now(), category, card.get("title"), card.get("company"), email, card.get("href")],
    )


def log_external_apply(category: str, card: dict, external_url: str):
    append_csv_row(
        EXTERNAL_APPLIES_FILE,
        ["timestamp", "category", "title", "company", "job_url", "external_apply_url"],
        [datetime.now(), category, card.get("title"), card.get("company"), card.get("href"), external_url],
    )


def match_recommended_category(label: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
    for category, aliases in RECOMMENDED_CATEGORIES.items():
        if normalized in aliases or any(
            re.fullmatch(rf"{re.escape(alias)}\s+\d+", normalized) for alias in aliases
        ):
            return category
    return None


def extract_emails(text: str) -> list[str]:
    return list(dict.fromkeys(email.lower() for email in EMAIL_PATTERN.findall(text or "")))


def resolve_external_apply_url(url: str | None, job_url: str) -> str:
    if not url:
        return ""
    resolved = urljoin(job_url, url.strip())
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.hostname and (parsed.hostname == "naukri.com" or parsed.hostname.endswith(".naukri.com")):
        return ""
    return resolved


def parse_posted_age_hours(posted_text: str | None) -> int | None:
    """Returns age in hours from strings like '1 day ago' or '4 hours ago'."""
    if not posted_text:
        return None
    text = posted_text.lower().strip()
    if "today" in text or "just now" in text:
        return 0
    match = re.search(r"(\d+)\s*(day|days|hour|hours|hr|hrs|week|weeks|month|months)", text)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    multiplier = {
        "hour": 1,
        "hours": 1,
        "hr": 1,
        "hrs": 1,
        "day": 24,
        "days": 24,
        "week": 168,
        "weeks": 168,
        "month": 720,
        "months": 720,
    }.get(unit)
    if multiplier is None:
        return None
    return value * multiplier


def classify_detail_label(text: str) -> str | None:
    """Maps a detail row's visible text to one of the four known match labels.

    Identification is by label text (robust to Naukri's "Keyskills" vs
    "Key Skills" spelling and to row order), NOT by row position.
    """
    normalized = " ".join((text or "").split())
    if not normalized:
        return None
    if re.search(r"early\s*applicant", normalized, re.IGNORECASE):
        return "Early Applicant"
    if re.search(r"keyskills?|key\s+skills?", normalized, re.IGNORECASE):
        return "Key Skills"
    if re.search(r"work\s*experience", normalized, re.IGNORECASE):
        return "Work Experience"
    if re.search(r"location", normalized, re.IGNORECASE) and not re.search(
        r"preferred\s*location", normalized, re.IGNORECASE
    ):
        return "Location"
    return None


def read_match_details(page) -> dict[str, str]:
    """Reads the four `.styles_MS__details__iS7mj` rows on the job detail page
    and classifies each by its label as PASS (`ni-icon-check_circle`), FAIL
    (the cross icon -- `ni-icon-crossMatchscore`) or UNKNOWN (no recognised
    icon). Returns a {label: status} map -- deliberately keyed by label so
    callers never have to assume which row is which.

    The row's icon classes are read back verbatim and matched by family
    ("check" -> PASS, "cross" -> FAIL) rather than an exact class name, so a
    Naukri rename (e.g. `ni-icon-matchcross` -> `ni-icon-crossMatchscore`)
    can't silently blind the check. If the known hashed container class ever
    changes, the rows are re-located by their label text.
    """
    rows = safe_evaluate(page, """
        () => {
            const hasIcon = (el) => el.querySelector('i[class*="ni-icon-"]');
            const known = /early\\s*applicant|keyskills?|key\\s+skills?|work\\s*experience|location/i;
            let rows = Array.from(document.querySelectorAll('.styles_MS__details__iS7mj'));
            if (!rows.length) {
                rows = Array.from(document.querySelectorAll('div, li'))
                    .filter(el => hasIcon(el))
                    .filter(el => {
                        const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                        return text.length < 40 && known.test(text);
                    });
            }
            return rows.map(el => ({
                label: (el.querySelector('span')?.innerText || el.innerText || '').replace(/\\s+/g, ' ').trim(),
                icons: Array.from(el.querySelectorAll('i')).map(i => i.className || '').join(' ').toLowerCase(),
            }));
        }
    """, default=[]) or []
    details: dict[str, str] = {}
    for row in rows:
        label = classify_detail_label(row.get("label", ""))
        if not label:
            continue
        icons = row.get("icons") or ""
        if "check" in icons:
            details[label] = MATCH_PASS
        elif "cross" in icons:
            details[label] = MATCH_FAIL
        else:
            details[label] = MATCH_UNKNOWN
    return details


def _detail_passed(value) -> bool:
    """True when a detail status means a positive match. Accepts the PASS
    string as well as a plain True so older boolean maps still work."""
    return value is True or (isinstance(value, str) and value.upper() == MATCH_PASS)


def summarize_match_status(match_status: dict) -> tuple[bool, str]:
    """Decides eligibility from the match-detail statuses.

    All four match rows must show a green check. A failed Key Skills row is
    tracked separately for profile optimisation; a missing or unknown Key
    Skills row is still ineligible because its match cannot be verified.
    """
    missing = [name for name in REQUIRED_MATCH_LABELS if not _detail_passed(match_status.get(name))]
    if not missing:
        return True, ""
    mismatched = [
        name for name in missing
        if match_status.get(name) is False
        or (isinstance(match_status.get(name), str) and match_status[name].upper() == MATCH_FAIL)
    ]
    if not mismatched:
        if "Key Skills" in missing:
            return False, "Key Skills did not show a green check"
        return False, "could not verify green-tick match status for: " + ", ".join(missing)
    return False, "green-tick mismatch: " + ", ".join(mismatched)


def passes_filters(card: dict) -> tuple[bool, str]:
    """Without a personal profile, only accept identifiable recommended jobs."""
    if not (card.get("title") or "").strip():
        return False, "job title could not be verified"
    if not (card.get("company") or "").strip():
        return False, "company could not be verified"
    return True, ""


def check_detail_page_filters(page, require_perfect_match: bool = True) -> tuple[bool, str]:
    """Require a perfect match score when requested and a posting age of at most 24h."""
    if require_perfect_match:
        match_ok, match_reason = check_match_score(page)
        if not match_ok:
            return False, match_reason

    posted_text = safe_evaluate(page, """
        () => {
            const text = document.body?.innerText || '';
            const match = text.match(
                /\\bPosted\\s*[:\\-]?\\s*(Today|Just now|\\d+\\s*(?:hours?|hrs?|days?|weeks?|months?)\\s*ago)\\b/i
            );
            return match ? match[0] : '';
        }
    """, default="") or ""
    age_hours = parse_posted_age_hours(posted_text)
    if age_hours is None:
        return False, "could not verify job posting age"
    if age_hours > 24:
        return False, f"job was posted {age_hours} hours ago (limit: 24 hours)"
    return True, ""


def check_match_score(page) -> tuple[bool, str]:
    """Returns whether all four recommendation match rows show a green check."""
    details = read_match_details(page)
    if details.get("Key Skills") == MATCH_FAIL:
        track_missing_keyskills(page)
    return summarize_match_status(details)


def extract_preferred_keyskills(page) -> list[str]:
    """Returns the names of the job's preferred keyskills -- the chips inside
    the `styles_key-skill__GIPn_` section that carry the `ni-icon-jd-save`
    icon. The same icon also appears once in the section legend (inside a
    <div>, not an <a>), so only anchors are considered to avoid picking up
    that legend sentence.
    """
    return safe_evaluate(page, """
        () => {
            const block = document.querySelector('.styles_key-skill__GIPn_');
            if (!block) return [];
            const names = [];
            for (const anchor of block.querySelectorAll('a')) {
                if (!anchor.querySelector('.ni-icon-jd-save')) continue;
                const span = anchor.querySelector('span');
                const name = (span?.innerText || anchor.innerText || '').replace(/\\s+/g, ' ').trim();
                if (name) names.push(name);
            }
            return Array.from(new Set(names));
        }
    """, default=[]) or []


def track_missing_keyskills(page) -> dict[str, int]:
    """Records every preferred keyskill on this job in the persistent
    skill-frequency map and returns the updated map. Called whenever the job's
    Key Skills row is a match-cross, BEFORE the apply/skip decision.
    """
    skills = extract_preferred_keyskills(page)
    if not skills:
        return skill_tracker.load_counts()
    counts = skill_tracker.record_missing(skills)
    print(f"  (Key Skills mismatch -- recorded for profile optimisation: {', '.join(skills)})")
    return counts


def safe_evaluate(page, script, arg=None, default=None):
    """Runs page.evaluate but never lets a destroyed-context or stray JS error
    crash the whole run -- returns `default` instead. Naukri's own chat widget
    and SPA navigation can tear down the page mid-script, which raises
    Playwright's 'Execution context was destroyed' error; that's expected
    occasionally, not something to crash over."""
    try:
        return page.evaluate(script, arg) if arg is not None else page.evaluate(script)
    except Exception as e:
        print(f"  (page.evaluate failed, continuing anyway: {e})")
        return default


def enumerate_cards(page):
    """Reads job cards from Naukri result pages, including recommendations."""
    return safe_evaluate(page, """
        () => {
            const found = new Map();
            const cards = Array.from(document.querySelectorAll(
                '.jobTuple[data-job-id], .srp-jobtuple-wrapper[data-job-id], article[data-job-id]'
            ));
            for (const card of cards) {
                const titleNode = card.querySelector('.jobTupleHeader .title, a.title, a[href*="/job-listings-"]');
                const title = (titleNode?.innerText || titleNode?.textContent || '').trim();
                const href = titleNode?.href || card.querySelector('a[href*="/job-listings-"]')?.href || '';
                const jobId = card.getAttribute('data-job-id') || href;
                if (!title || !jobId || found.has(jobId)) continue;
                const text = selector => (card?.querySelector(selector)?.innerText || '').trim();
                found.set(jobId, {
                    jobId,
                    title,
                    href,
                    company: text('.companyInfo .companyWrapper [title], a.comp-name, .comp-dtls-wrap a'),
                    exp: text('.experience span[title], .expwdth, [class*="experience"]'),
                    location: text('.location span[title], .locWdth, [class*="location"]'),
                });
            }
            return Array.from(found.values());
        }
    """, default=[]) or []


def hide_recommended_job(page, job_id: str) -> bool:
    escaped_job_id = str(job_id).replace("\\", "\\\\").replace('"', '\\"')
    card = page.locator(f'article.jobTuple[data-job-id="{escaped_job_id}"]').first
    if not card.count():
        return False
    try:
        card.hover(timeout=2000)
    except Exception:
        pass

    footer_hide = card.locator(
        ".jobTupleFooter .saveJobContainer:has(.naukicon-ot-hide)"
    )
    if footer_hide.count():
        footer_hide.evaluate("element => element.click()")
        try:
            page.wait_for_function("""
                jobId => !Array.from(document.querySelectorAll('article.jobTuple[data-job-id]'))
                    .some(card => card.getAttribute('data-job-id') === jobId)
            """, arg=str(job_id), timeout=2500)
            return True
        except PWTimeout:
            return False

    # Newer recommendation cards expose the hide control as a span carrying
    # this exact class bundle (from the "Hide this job" control). Guard against
    # clicking the sibling "save" control: only click when the label is hide-ish
    # or blank, so a job is never accidentally saved while trying to hide it.
    spec_hide = card.locator(".dspIB.valignM.saveSpn.typ-11Medium.mr-16, .saveSpn")
    if spec_hide.count():
        element = spec_hide.first
        try:
            label = (element.inner_text() or "").strip().lower()
        except Exception:
            label = ""
        if not label or re.search(r"hide|not interested|don'?t show|remove", label):
            element.evaluate("element => element.click()")
            try:
                page.wait_for_function("""
                    jobId => !Array.from(document.querySelectorAll('article.jobTuple[data-job-id]'))
                        .some(card => card.getAttribute('data-job-id') === jobId)
                """, arg=str(job_id), timeout=2500)
                return True
            except PWTimeout:
                return not card.count()

    labeled_control = card.locator(
        'button[aria-label*="hide" i], [role="button"][aria-label*="hide" i], '
        'button[title*="hide" i], [role="button"][title*="hide" i], a[title*="hide" i]'
    )
    if labeled_control.count():
        labeled_control.first.evaluate("element => element.click()")
        return not card.count()

    text_control = card.get_by_text(re.compile(r"^\s*hide(?:\s+this\s+job)?\s*$", re.IGNORECASE))
    if text_control.count():
        text_control.first.evaluate("element => element.click()")
        return not card.count()
    return False


def hide_rejected_cards_on_page(page, cards: list[dict], rejected_ids: set[str], category: str) -> int:
    hidden_count = 0
    for card in rejected_cards(cards, rejected_ids):
        job_id = str(card.get("jobId") or "")
        if not job_id:
            continue
        try:
            if hide_recommended_job(page, job_id):
                hidden_count += 1
                print(f"Hidden from {category}: {card.get('title')} ({job_id})")
            else:
                print(f"Hide control not found in {category}: {card.get('title')} ({job_id})")
        except Exception as exc:
            print(f"Could not hide from {category} {card.get('title')} ({job_id}): {exc}")
    return hidden_count


def hide_rejected_jobs_in_categories(page, max_scrolls: int, rejected_ids: set[str]) -> int:
    hidden_count = 0
    if not rejected_ids:
        print("No unsuitable jobs are recorded; nothing to hide.")
        return hidden_count

    for category in RECOMMENDED_CATEGORIES:
        page.goto(RECOMMENDED_JOBS_URL)
        time.sleep(1.5)
        stop = page_has_stop_signal(page)
        if stop:
            raise StopRun(f"stop signal while hiding rejected jobs: {stop}")
        if not open_recommended_category(page, category):
            print(f"Category missing or did not activate while hiding: {category}")
            continue
        try:
            page.wait_for_selector('.jobTuple[data-job-id], .srp-jobtuple-wrapper[data-job-id]', timeout=8000)
        except PWTimeout:
            pass

        matches = rejected_cards(collect_recommended_cards(page, max_scrolls), rejected_ids)
        print(f"\n--- Hide pass: {category} ({len(matches)} recorded unsuitable jobs) ---")
        hidden_count += hide_rejected_cards_on_page(page, matches, rejected_ids, category)
        if matches:
            time.sleep(0.4)
    return hidden_count


def hide_jobs_by_match_score_in_categories(page, max_scrolls: int,
                                           rejected_ids: set[str]) -> int:
    """Hide recommendation cards unless all four match rows show green checks."""
    hidden_count = 0
    for category in RECOMMENDED_CATEGORIES:
        page.goto(RECOMMENDED_JOBS_URL)
        time.sleep(1.5)
        stop = page_has_stop_signal(page)
        if stop:
            raise StopRun(f"stop signal while checking match scores: {stop}")
        if not open_recommended_category(page, category):
            print(f"Category missing or did not activate while checking match scores: {category}")
            continue
        try:
            page.wait_for_selector('.jobTuple[data-job-id], .srp-jobtuple-wrapper[data-job-id]', timeout=8000)
        except PWTimeout:
            pass

        cards = collect_recommended_cards(page, max_scrolls)
        print(f"\n--- Match-score hide pass: {category} ({len(cards)} jobs) ---")
        for card in cards:
            job_id = str(card.get("jobId") or "")
            if not job_id:
                continue

            detail_page = None
            try:
                detail_page = open_recommended_job(page, card)
                time.sleep(2)
                stop = page_has_stop_signal(detail_page)
                if stop:
                    raise StopRun(f"stop signal while checking match score: {stop}")
                match_ok, reason = check_match_score(detail_page)
                if match_ok:
                    continue

                record_rejected_job(job_id, category, reason)
                rejected_ids.add(job_id)
                detail_page.close()
                detail_page = None
                if hide_recommended_job(page, job_id):
                    hidden_count += 1
                    print(f"Hidden from {category}: {card.get('title')} ({reason})")
                else:
                    print(f"Hide control not found in {category}: {card.get('title')} ({job_id})")
            except StopRun:
                raise
            except SkipJob as exc:
                print(f"Could not check match score for {card.get('title')} ({job_id}): {exc}")
            except Exception as exc:
                print(f"Could not check or hide {card.get('title')} ({job_id}): {exc}")
            finally:
                if detail_page and not detail_page.is_closed():
                    detail_page.close()
        time.sleep(0.4)
    return hidden_count


def open_recommended_job(page, card: dict):
    selector = f'article.jobTuple[data-job-id="{card["jobId"]}"] .title'
    try:
        with page.expect_popup(timeout=8000) as popup_info:
            page.locator(selector).click(timeout=6000)
        detail_page = popup_info.value
    except PWTimeout as exc:
        raise SkipJob(f"couldn't open job detail tab for {card.get('title')}") from exc
    try:
        detail_page.wait_for_load_state("domcontentloaded", timeout=10000)
    except PWTimeout:
        pass
    return detail_page


def open_recommended_category(page, category: str) -> bool:
    tabs = safe_evaluate(page, """
        () => Array.from(document.querySelectorAll('.tabs-container .tab-list-item')).map((el, index) => ({
            index,
            label: (el.innerText || el.textContent || '').trim(),
            active: el.classList.contains('tab-list-active')
        })).filter(tab => tab.label)
    """, default=[]) or []
    match = next(
        (tab for tab in tabs if match_recommended_category(tab["label"]) == category),
        None,
    )
    if not match:
        return False
    if match["active"]:
        return True

    page.locator(".tabs-container .tab-list-item").nth(match["index"]).click()
    try:
        page.wait_for_function(
            "index => document.querySelectorAll('.tabs-container .tab-list-item')[index]?.classList.contains('tab-list-active')",
            arg=match["index"],
            timeout=7000,
        )
    except PWTimeout:
        return False
    page.wait_for_timeout(1200)
    return True


def refresh_category_page(page, category: str) -> bool:
    """Reloads the recommendation category page and re-activates its tab so the
    next job card is read from fresh DOM. Without this the category page can
    keep the previously-opened job's data around, which the next card would be
    matched against. Best-effort: failures here never abort the run.
    """
    try:
        page.reload(wait_until="domcontentloaded", timeout=15000)
    except Exception:
        try:
            page.goto(RECOMMENDED_JOBS_URL, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            return False
    time.sleep(1.0)
    try:
        open_recommended_category(page, category)
    except Exception:
        pass
    try:
        page.wait_for_selector(
            '.jobTuple[data-job-id], .srp-jobtuple-wrapper[data-job-id]', timeout=8000
        )
    except Exception:
        pass
    return True


def collect_recommended_cards(page, max_scrolls: int) -> list[dict]:
    cards_by_id = {}
    stagnant_scrolls = 0
    for _ in range(max(1, max_scrolls)):
        for card in enumerate_cards(page):
            cards_by_id[card["jobId"]] = card
        before = len(cards_by_id)
        safe_evaluate(page, "() => window.scrollTo(0, document.body.scrollHeight)", default=None)
        page.wait_for_timeout(900)
        for card in enumerate_cards(page):
            cards_by_id[card["jobId"]] = card
        if len(cards_by_id) == before:
            stagnant_scrolls += 1
            if stagnant_scrolls >= 2:
                break
        else:
            stagnant_scrolls = 0
    return list(cards_by_id.values())


def get_job_description(page) -> str:
    return safe_evaluate(page, """
        () => {
            const selectors = [
                '[class*="dang-inner-html"]', '#jobDescription',
                '[itemprop="description"]', '[class*="jobDescription"]',
                '[class*="job-description"]'
            ];
            for (const selector of selectors) {
                const nodes = Array.from(document.querySelectorAll(selector));
                const text = nodes.map(node => node.innerText || '').filter(Boolean).join(String.fromCharCode(10));
                if (text.trim()) return text;
            }
            return '';
        }
    """, default="") or ""


def get_external_apply_url(page, job_url: str) -> str:
    raw_url = safe_evaluate(page, """
        () => {
            const button = document.querySelector('#company-site-button');
            if (!button) return '';
            const anchor = button.matches('a') ? button : button.closest('a') || button.querySelector('a[href]');
            const candidates = [
                anchor?.href,
                button.getAttribute('data-url'),
                button.getAttribute('data-href'),
                button.getAttribute('data-redirect-url'),
                button.getAttribute('formaction'),
                button.getAttribute('href'),
            ];
            return candidates.find(value => value && /^https?:|^\//i.test(value)) || '';
        }
    """, default="")
    resolved = resolve_external_apply_url(raw_url, job_url)
    if resolved:
        return resolved

    try:
        with page.expect_popup(timeout=2500) as popup_info:
            page.locator("#company-site-button").click(timeout=2000)
        popup = popup_info.value
        external_url = resolve_external_apply_url(popup.url, job_url)
        popup.close()
        return external_url
    except PWTimeout:
        external_url = resolve_external_apply_url(page.url, job_url)
        if external_url:
            page.goto(job_url)
        return external_url
    except Exception as exc:
        print(f"  (couldn't capture external apply destination: {exc})")
        return ""


def check_apply_button(page) -> str:
    """Returns 'external', 'native', or 'none' based on the detail page's apply button."""
    if page.query_selector('#company-site-button'):
        return "external"
    if page.query_selector('#apply-button'):
        return "native"
    return "none"


def _wait_send_enabled(page, timeout_ms: int = 4000) -> bool:
    """Polls until the Send control's wrapper no longer has Naukri's
    'disabled' class. Confirmed real markup (from a debug HTML dump you
    sent): <div id="sendMsg__..." class="send disabled"> wraps the
    clickable <div class="sendMsg">Save</div>. It starts disabled and only
    becomes clickable after Naukri's frontend registers your selection and
    re-renders -- clicking Send before that happens is a silent no-op,
    which is what was causing radio/checkbox answers to never actually save."""
    waited = 0
    step = 300
    while waited <= timeout_ms:
        enabled = safe_evaluate(page, """
            () => {
                const wrapper = document.querySelector('[id^="sendMsg__"]');
                if (!wrapper) return true;  // no wrapper found -- don't block forever on a guess
                return !wrapper.className.includes('disabled');
            }
        """, default=True)
        if enabled:
            return True
        time.sleep(step / 1000)
        waited += step
    return False


def _js_click_send(page) -> bool:
    """Clicks Naukri's screening-chat Send/Save control -- a <div class="sendMsg">,
    not a <button>. A JS click bypasses the chatbot_Overlay div that sits
    visually on top of it and blocks Playwright's normal .click()."""
    return safe_evaluate(page, """
        () => {
            const el = document.querySelector('.sendMsg');
            if (!el) return false;
            el.click();
            return true;
        }
    """, default=False)


def click_native_apply(page) -> bool:
    """Clicks the Apply button and returns whether the click actually
    registered, so the caller can tell 'click failed' apart from 'clicked
    fine, just couldn't confirm success afterward' -- those need different
    handling."""
    return bool(safe_evaluate(page, """
        () => {
            const btn = document.getElementById('apply-button');
            if (btn) { btn.click(); return true; }
            return false;
        }
    """, default=False))


def page_has_stop_signal(page) -> str | None:
    try:
        text = page.inner_text("body").lower()
    except Exception:
        return None  # page mid-navigation -- checked again on the next loop iteration
    for phrase in STOP_PHRASES:
        if phrase in text:
            return phrase
    if "login" in page.url and "naukri.com/nlogin" in page.url:
        return "session expired / login prompt"
    return None


def _save_debug_screenshot(page, job_title: str) -> str:
    """Saves a screenshot when the outcome is uncertain, so it can be looked
    at afterward instead of needing to catch it live. Also saves the page's
    HTML for the same reason — a screenshot shows what it looked like, the
    HTML shows exactly why the confirmation-text check missed it."""
    debug_dir = data_path("debug_screenshots")
    debug_dir.mkdir(exist_ok=True)
    safe_name = "".join(c if c.isalnum() else "_" for c in (job_title or "unknown"))[:60]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_path = debug_dir / f"{safe_name}_{timestamp}.png"
    html_path = debug_dir / f"{safe_name}_{timestamp}.html"
    try:
        page.screenshot(path=str(png_path))
    except Exception as e:
        print(f"  (couldn't save debug screenshot: {e})")
    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception as e:
        print(f"  (couldn't save debug HTML: {e})")
    return str(png_path)


def verify_applied(page) -> bool:
    """Best-effort check that the application actually went through. Confirmed
    real pattern: Naukri shows a green checkmark panel reading
    'Applied to "<job title>"' a few seconds after submit -- checking for
    that prefix specifically, plus a short wait since it isn't instant."""
    try:
        page.wait_for_timeout(3000)  # the confirmation panel takes a moment to appear
    except Exception:
        pass
    try:
        text = page.inner_text("body").lower()
    except Exception:
        return False
    success_phrases = [
        'applied to "', "application sent", "successfully applied",
        "you have applied", "applied successfully",
    ]
    if any(p in text for p in success_phrases):
        return True
    btn = page.query_selector('#apply-button')
    if btn:
        try:
            label = btn.inner_text().strip().lower()
            if label and label != "apply":
                return True
        except Exception:
            pass
    return False


class StopRun(Exception):
    """Something genuinely dangerous or account-risking happened -- CAPTCHA,
    login expired, rate-limit warning. Halts the entire run immediately."""


class SkipJob(Exception):
    """This one listing can't be completed -- logs it and moves to the next
    listing. Does NOT stop the run."""


def _get_options(page) -> list[dict]:
    """Reads radio/checkbox options in the chat drawer, with their visible labels."""
    return safe_evaluate(page, """
        () => {
            const root = document.querySelector('[class*="chatbot_Drawer"]') || document;
            const inputs = Array.from(root.querySelectorAll('input[type=radio], input[type=checkbox]'));
            return inputs.map((el, idx) => {
                let label = '';
                if (el.id) {
                    const lbl = root.querySelector(`label[for="${el.id}"]`);
                    if (lbl) label = lbl.innerText.trim();
                }
                if (!label) {
                    const parentLabel = el.closest('label');
                    if (parentLabel) label = parentLabel.innerText.trim();
                }
                if (!label && el.nextElementSibling) {
                    label = (el.nextElementSibling.innerText || '').trim();
                }
                return {
                    idx,
                    label,
                    type: el.type,
                    selected: !!el.checked || el.getAttribute('aria-checked') === 'true'
                };
            });
        }
    """, default=[]) or []


def _click_option(page, idx: int) -> bool:
    return safe_evaluate(page, """
        (idx) => {
            const root = document.querySelector('[class*="chatbot_Drawer"]') || document;
            const inputs = Array.from(root.querySelectorAll('input[type=radio], input[type=checkbox]'));
            if (inputs[idx]) { inputs[idx].click(); return true; }
            return false;
        }
    """, arg=idx, default=False)


def _pause_for_manual_input(page, question: str):
    page.bring_to_front()
    print("\nAnswer in the visible Naukri browser; the workflow will continue automatically.")
    print(f"Question: {question}")
    while True:
        if page.is_closed():
            raise SkipJob("the browser was closed before the screening question was answered")
        options = _get_options(page)
        selected = [opt["label"].strip() for opt in options
                    if opt.get("selected") and opt["label"].strip()]
        response = _read_filled_text(page)
        if selected or response:
            return
        stop = page_has_stop_signal(page)
        if stop:
            raise StopRun(f"stop signal while waiting for a screening answer: {stop}")
        page.wait_for_timeout(300)


def _handle_options_question(page, question: str) -> str | None:
    """Select a remembered answer or wait for a selection in the browser."""
    options = _get_options(page)
    if not options:
        return None

    stored = learned_answers.get_answer(question)
    if stored:
        for opt in options:
            if opt["label"].strip().lower() == stored.strip().lower():
                _click_option(page, opt["idx"])
                print(f"  (used a remembered answer for: {question[:80]})")
                return stored

    _pause_for_manual_input(page, question)
    selected = [opt["label"].strip() for opt in _get_options(page) if opt.get("selected") and opt["label"].strip()]
    if not selected:
        raise SkipJob(f"no option selected in the browser for: {question[:120]}")
    return selected[0] if len(selected) == 1 else ", ".join(selected)


# If the chat's last message is one of these, it's wrapping up rather than
# asking something new -- stop cleanly instead of trying to draft an answer
# into a box that may no longer exist.
COMPLETION_PHRASES = [
    "thank you", "thanks for your response", "thanks for your time",
    "we will get back", "responses have been recorded", "no further questions",
    "application submitted", "that's all", "all the information we need",
]


def _is_completion_message(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in COMPLETION_PHRASES)


def _has_answerable_input(page, timeout_ms: int = 3000) -> bool:
    """Checks for a text box or radio/checkbox to answer. Polls for up to
    timeout_ms instead of checking once -- the previous single-check version
    could wrongly conclude "nothing to answer" if the next question's input
    just hadn't rendered yet after the previous Save click, causing it to
    skip clicking Save on what was actually still a real question."""
    waited = 0
    step = 300
    while waited <= timeout_ms:
        found = safe_evaluate(page, """
            () => !!(document.querySelector('[id^="userInput"], [contenteditable="true"]') ||
                     document.querySelector('input[type=radio], input[type=checkbox]'))
        """, default=False)
        if found:
            return True
        time.sleep(step / 1000)
        waited += step
    return False


def _read_filled_text(page) -> str:
    return safe_evaluate(page, """
        () => {
            const root = document.querySelector('[class*="chatbot_Drawer"]') || document;
            const ed = root.querySelector(
                '[id^="userInput"], [contenteditable="true"], textarea, input[type="text"], input[type="number"], input[type="email"]'
            );
            return ed ? (ed.value || ed.innerText || ed.textContent || '').trim() : '';
        }
    """, default="") or ""


def _send_current_answer(page, question: str):
    if not _read_filled_text(page):
        raise SkipJob(f"couldn't confirm a manual answer was entered: {question[:120]}")
    if not _wait_send_enabled(page):
        raise SkipJob(f"Send button stayed disabled after answering: {question[:120]}")
    confirmation_state = _answer_confirmation_state(page)
    if not _js_click_send(page):
        raise SkipJob(f"couldn't find the Send control after answering: {question[:120]}")
    _wait_for_answer_confirmation(page, question, confirmation_state)


def _answer_confirmation_state(page) -> dict:
    return safe_evaluate(page, """
        () => {
            const root = document.querySelector('[class*="chatbot_Drawer"]') || document;
            const messages = Array.from(root.querySelectorAll('.botMsg'));
            const successIcons = root.querySelectorAll(
                '[class*="success"], [class*="check_circle"], [aria-label*="success" i], [data-testid*="success" i]'
            );
            return {
                latestMessage: (messages[messages.length - 1]?.innerText || '').trim(),
                successIconCount: successIcons.length
            };
        }
    """, default={"latestMessage": "", "successIconCount": 0})


def _wait_for_answer_confirmation(page, question: str, previous_state: dict):
    """Wait until the chatbot acknowledges the answer or presents its next prompt."""
    try:
        page.wait_for_function("""
            (previous) => {
                const root = document.querySelector('[class*="chatbot_Drawer"]') || document;
                const messages = Array.from(root.querySelectorAll('.botMsg'));
                const latest = (messages[messages.length - 1]?.innerText || '').trim();
                const normalize = value => value.replace(/\\s+/g, ' ').trim().toLowerCase();
                if (latest && normalize(latest) !== normalize(previous.latestMessage)) return true;
                if (messages.length && /thank you|thanks for your response|responses have been recorded|application submitted|no further questions/i.test(latest)) return true;
                const successIcons = root.querySelectorAll(
                    '[class*="success"], [class*="check_circle"], [aria-label*="success" i], [data-testid*="success" i]'
                );
                return successIcons.length > previous.successIconCount;
            }
        """, arg=previous_state, timeout=15000)
    except PWTimeout as exc:
        raise SkipJob(
            f"the screening answer was sent, but the browser did not confirm success: {question[:120]}"
        ) from exc
    print(f"  [SUCCESS] Answer confirmed: {question[:80]}")


def _fill_and_send(page, text: str, question: str):
    """Fills the answer box, VERIFIES the text actually landed before
    clicking Send, then sends. This is the fix for answers going through
    blank: previously Send could fire even if the fill silently failed
    (a timing hiccup, or the box wasn't there), which is what produces
    Naukri's "incomplete information" rejection on the final application."""
    _fill_freetext(page, text)
    time.sleep(0.4)
    filled = _read_filled_text(page)
    if not filled:
        _fill_freetext(page, text)  # one retry -- could be a focus/timing hiccup
        time.sleep(0.6)
        filled = _read_filled_text(page)
    if not filled:
        learned_answers.save_unanswered(question)
        raise SkipJob(f"couldn't confirm the answer registered before sending: {question[:120]}")
    _send_current_answer(page, question)


def answer_screening_chat(page):
    """Handles Naukri's post-apply screening chat drawer, if it appears."""
    try:
        # Broadened on purpose: a pure radio/checkbox question has no text
        # box at all, so waiting only for contenteditable was causing the
        # function to give up immediately on those questions, thinking
        # there was no screening chat when there actually was one.
        page.wait_for_selector(
            '[contenteditable="true"], [contenteditable=""], '
            'input[type=radio], input[type=checkbox], .botMsg',
            timeout=4000,
        )
    except PWTimeout:
        return  # no screening chat for this listing

    for _ in range(15):
        stop = page_has_stop_signal(page)
        if stop:
            raise StopRun(f"stop signal during screening chat: {stop}")

        bubbles = page.query_selector_all('.botMsg')
        if not bubbles:
            break
        try:
            question = bubbles[-1].inner_text().strip()
        except Exception:
            break  # page likely navigated away mid-read; treat as chat finished
        if not question:
            break

        if _is_completion_message(question):
            time.sleep(1.5)  # let the "Applied" confirmation redirect begin before we check for it
            break  # chat is wrapping up, not asking a new question

        if not _has_answerable_input(page):
            break  # nothing to fill or click here — informational message only

        options = _get_options(page)
        if options:
            answer = _handle_options_question(page, question)
            if not _wait_send_enabled(page):
                raise SkipJob(f"Send button stayed disabled after selecting an option "
                               f"(selection may not have registered): {question[:120]}")
            confirmation_state = _answer_confirmation_state(page)
            if not _js_click_send(page):
                raise SkipJob(f"couldn't find the Send control for: {question[:120]}")
            _wait_for_answer_confirmation(page, question, confirmation_state)
            if answer:
                learned_answers.save_answer(question, answer)
            continue

        stored = learned_answers.get_answer(question)
        if stored:
            _fill_and_send(page, stored, question)
            print(f"  (used a remembered answer for: {question[:80]})")
            continue

        _pause_for_manual_input(page, question)
        response = _read_filled_text(page)
        _send_current_answer(page, question)
        learned_answers.save_answer(question, response)


def _fill_freetext(page, text: str):
    safe_evaluate(page, """
        (text) => {
            const ed = document.querySelector('[id^="userInput"], [contenteditable="true"]');
            if (!ed) return;
            ed.focus();
            document.execCommand('insertText', false, text);
        }
    """, arg=text)


def run(hide_rejected_only: bool = False, hide_rejected: bool = True,
        match_score_mode: str = "perfect", hide_by_match_score: bool = False,
        session_file: str | Path = SESSION_FILE):
    if match_score_mode not in {"perfect", "any"}:
        raise ValueError("match_score_mode must be 'perfect' or 'any'")
    session_path = Path(session_file)
    if not session_path.exists():
        raise SystemExit(
            f"{session_path} not found. Run: python cli.py and choose option 1 "
            f"(or run python login_capture.py naukri)"
        )

    max_scrolls = DEFAULT_MAX_SCROLLS

    applied = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=150)
        context = browser.new_context(storage_state=str(session_path))
        page = context.new_page()
        seen_job_ids = load_visited_job_ids()
        rejected_job_ids = load_rejected_job_ids()
        previously_handled_job_keys = load_previously_handled_job_keys()
        processed_job_ids = set()
        visited_categories = set()

        page.goto("https://www.naukri.com/mnjuser/homepage")
        time.sleep(1.5)
        stop = page_has_stop_signal(page)
        if stop:
            print(f"STOPPING: {stop}")
            log_row([datetime.now(), "naukri", "-", "-", "stopped", stop])
            save_session_state(context, session_path)
            browser.close()
            return

        if hide_by_match_score:
            try:
                hidden_count = hide_jobs_by_match_score_in_categories(
                    page, max_scrolls, rejected_job_ids
                )
                print(f"Match-score hide pass complete. {hidden_count} jobs hidden.")
            except StopRun as exc:
                print(f"STOPPING: {exc}")
                log_row([datetime.now(), "naukri", "-", "-", "stopped", str(exc)])
            finally:
                save_session_state(context, session_path)
                browser.close()
            return

        if hide_rejected_only:
            try:
                hidden_count = hide_rejected_jobs_in_categories(page, max_scrolls, rejected_job_ids)
                print(f"Hide pass complete. {hidden_count} cards hidden.")
            except StopRun as exc:
                print(f"STOPPING: {exc}")
                log_row([datetime.now(), "naukri", "-", "-", "stopped", str(exc)])
            finally:
                save_session_state(context, session_path)
                browser.close()
            return

        for category in RECOMMENDED_CATEGORIES:
            if category in visited_categories:
                print(f"Skipping already visited category: {category}")
                continue

            page.goto(RECOMMENDED_JOBS_URL)
            time.sleep(1.5)
            stop = page_has_stop_signal(page)
            if stop:
                print(f"STOPPING: {stop}")
                log_row([datetime.now(), "naukri", "-", "-", "stopped", stop])
                save_session_state(context, session_path)
                browser.close()
                return
            if not open_recommended_category(page, category):
                print(f"Category missing or did not activate on Recommended Jobs page: {category}")
                continue
            visited_categories.add(category)

            try:
                page.wait_for_selector('.jobTuple[data-job-id], .srp-jobtuple-wrapper[data-job-id]', timeout=8000)
            except PWTimeout:
                pass
            cards = collect_recommended_cards(page, max_scrolls)
            if hide_rejected and match_score_mode == "perfect":
                hide_rejected_cards_on_page(page, cards, rejected_job_ids, category)
            candidate_cards = []
            for card in cards:
                job_id = str(card.get("jobId") or "")
                job_key = normalize_job_key(card.get("title", ""), card.get("company", ""))
                skip_rejected = match_score_mode == "perfect" and job_id in rejected_job_ids
                if (not job_id or job_id in processed_job_ids or skip_rejected
                        or job_key in previously_handled_job_keys):
                    continue
                candidate_cards.append(card)
            print(f"\n--- Recommended category: {category} ({len(candidate_cards)} candidates to check) ---")

            for card in candidate_cards:
                job_id = str(card.get("jobId") or "")
                if not job_id or job_id in processed_job_ids:
                    continue
                processed_job_ids.add(job_id)
                seen_job_ids.add(job_id)
                save_visited_job_ids(seen_job_ids)

                ok, reason = passes_filters(card)
                if not ok:
                    record_rejected_job(job_id, category, reason)
                    rejected_job_ids.add(job_id)
                    if hide_rejected:
                        hide_rejected_cards_on_page(page, [card], rejected_job_ids, category)
                    log_row([datetime.now(), "naukri", card.get("title"),
                              card.get("company"), "skipped", reason])
                    print(f"Skipped: {card.get('title')} @ {card.get('company')} -- {reason}")
                    continue

                detail_page = None
                try:
                    detail_page = open_recommended_job(page, card)
                    card["href"] = detail_page.url
                    time.sleep(2)

                    stop = page_has_stop_signal(detail_page)
                    if stop:
                        raise StopRun(f"stop signal: {stop}")

                    fit_ok, fit_reason = check_detail_page_filters(
                        detail_page,
                        require_perfect_match=match_score_mode == "perfect",
                    )
                    if not fit_ok:
                        if is_explicit_detail_rejection(fit_reason):
                            record_rejected_job(job_id, category, fit_reason)
                            rejected_job_ids.add(job_id)
                            if hide_rejected:
                                hide_rejected_cards_on_page(page, [card], rejected_job_ids, category)
                        log_row([datetime.now(), "naukri", card.get("title"),
                                 card.get("company"), "skipped", fit_reason])
                        print(f"Skipped: {card.get('title')} @ {card.get('company')} -- {fit_reason}")
                        continue

                    if is_already_applied(detail_page):
                        previously_handled_job_keys.add(normalize_job_key(
                            card.get("title", ""), card.get("company", "")
                        ))
                        log_row([datetime.now(), "naukri", card.get("title"),
                                 card.get("company"), "skipped", "already applied"])
                        print(f"Skipped: {card.get('title')} @ {card.get('company')} -- already applied")
                        continue

                    for email in extract_emails(get_job_description(detail_page)):
                        log_recruiter_email(category, card, email)

                    apply_state = check_apply_button(detail_page)
                    if apply_state == "external":
                        external_url = get_external_apply_url(detail_page, card["href"])
                        log_external_apply(category, card, external_url)
                        reason = "external apply" if external_url else "external apply URL not exposed"
                        log_row([datetime.now(), "naukri", card.get("title"),
                                 card.get("company"), "skipped", reason])
                        print(f"External apply: {card.get('title')} @ {card.get('company')} "
                              f"({external_url or 'URL unavailable'})")
                        continue
                    if apply_state == "none":
                        log_row([datetime.now(), "naukri", card.get("title"),
                                 card.get("company"), "skipped", "no apply button found"])
                        print(f"Skipped: {card.get('title')} @ {card.get('company')} -- no apply button found")
                        continue

                    clicked = click_native_apply(detail_page)
                    if not clicked:
                        log_row([datetime.now(), "naukri", card.get("title"),
                                 card.get("company"), "skipped", "apply button click didn't register"])
                        print(f"Skipped: {card.get('title')} @ {card.get('company')} -- apply click didn't register")
                        continue
                    time.sleep(2)

                    answer_screening_chat(detail_page)
                    time.sleep(1.5)

                    if verify_applied(detail_page):
                        applied += 1
                        previously_handled_job_keys.add(normalize_job_key(
                            card.get("title", ""), card.get("company", "")
                        ))
                        log_row([datetime.now(), "naukri", card.get("title"),
                                 card.get("company"), "applied", ""])
                        print(f"Applied: {card.get('title')} @ {card.get('company')} ({applied} total)")
                    else:
                        previously_handled_job_keys.add(normalize_job_key(
                            card.get("title", ""), card.get("company", "")
                        ))
                        shot_path = _save_debug_screenshot(detail_page, card.get("title"))
                        log_row([datetime.now(), "naukri", card.get("title"),
                                 card.get("company"), "uncertain",
                                 f"couldn't confirm submission -- screenshot saved to {shot_path}"])
                        print(f"UNCERTAIN: {card.get('title')} @ {card.get('company')} -- "
                              f"couldn't confirm the application actually went through. Check it manually.")

                except SkipJob as e:
                    log_row([datetime.now(), "naukri", card.get("title"),
                             card.get("company"), "skipped", str(e)])
                    print(f"Skipped: {card.get('title')} @ {card.get('company')} -- {e}")
                    continue

                except StopRun as e:
                    print(f"STOPPING: {e}")
                    log_row([datetime.now(), "naukri", card.get("title"),
                             card.get("company"), "stopped", str(e)])
                    save_session_state(context, session_path)
                    browser.close()
                    return

                except Exception as e:
                    log_row([datetime.now(), "naukri", card.get("title"),
                             card.get("company"), "skipped", f"unexpected error: {e}"])
                    print(f"Skipped (unexpected error): {card.get('title')} @ {card.get('company')} -- {e}")
                    continue
                finally:
                    if detail_page and not detail_page.is_closed():
                        detail_page.close()
                    # Return to the category page with a clean slate so the next
                    # card isn't evaluated against the previous job's stale data.
                    if not page.is_closed():
                        refresh_category_page(page, category)

                time.sleep(DEFAULT_ACTION_DELAY_SECONDS)

        save_session_state(context, session_path)
        browser.close()

    print(f"\nDone. {applied} applications submitted. See {LOG_FILE}, "
          f"{RECRUITER_EMAILS_FILE}, and {EXTERNAL_APPLIES_FILE} for results.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply to eligible Naukri recommendations.")
    parser.add_argument(
        "--hide-rejected", action="store_true",
        help="Only revisit recommendation categories and hide recorded green-tick mismatches.",
    )
    parser.add_argument(
        "--no-hide", action="store_true",
        help="Apply to jobs but never hide rejected/mismatched cards.",
    )
    args = parser.parse_args()
    run(hide_rejected_only=args.hide_rejected, hide_rejected=not args.no_hide)
