# Naukri Auto-Apply

A personal automation tool that applies to Naukri Recommended Jobs using
your own browser session. The Naukri runner uses conservative built-in
limits and asks you to answer screening questions in the visible browser;
it does not require a profile file or an AI provider.

Also includes a resume refresh script (remove + re-upload, to keep your
profile showing as recently active) and an optional mobile-friendly status
dashboard.

## Read this before you use it

**This automates actions against Naukri's terms of service.** Naukri (like
most job platforms) prohibits automated applying, and can detect and
suspend accounts for bot-like activity. This tool tries to reduce that risk
(pacing between actions, stopping instantly on any CAPTCHA or rate-limit
warning, using a real logged-in browser session rather than headless
scraping) but it cannot eliminate the risk. You are running this against
your own account, at your own discretion, and the consequences are yours
to weigh.

**The script never sees or stores your password.** You log into Naukri by
hand in a real browser window; the script only reuses the resulting
session cookies.

**Datacenter IPs get blocked.** Naukri's infrastructure blocks traffic
from cloud-hosting IP ranges (AWS, Oracle Cloud, GCP, etc.) at the network
level. Run this from a residential IP -- your own computer, phone, or a
device on your home network -- not a rented cloud server.

## What it does

- Opens your Naukri Recommended Jobs page and visits Applies, Profile,
  Top Candidate, Preference, and You Might Like
- Records visited job IDs in `visited_recommended_jobs.json`; later runs
  recheck them unless the job is rejected or has an applied/uncertain outcome
- Records explicit unsuitable-match IDs in `rejected_recommended_jobs.csv`
  and hides those cards as each recommendation category is visited
- Applies only when all four match checks pass -- Early Applicant,
  Location, Work Experience, and Key Skills must all show a green check
  (`ni-icon-check_circle`) and the job must have been posted within the last
  24 hours. Each match row is identified by its label text, never by position,
  so it survives Naukri reordering the rows. A cross icon or missing/unknown
  Key Skills check blocks applying and records the job for hiding; failed
  Key Skills are also tallied in `missing_skills.json` for profile optimisation.
- Processes one job at a time, each in its own tab; once a job is finished (or
  skipped) its tab is closed and the category page is reloaded before the next
  card is handled, so a job is never judged against the previous job's stale
  page state
- Processes every discovered eligible job across all recommendation
  categories, with a 5-second delay between jobs and up to 5
  recommendation-page scrolls per category.
  Without a profile file, role, company, and experience preferences are not
  filtered; only Naukri-recommended jobs with verified required green checks
  and a posting age of at most 24 hours are eligible.
- Asks you to enter or select every screening answer in the visible browser
  unless an exact answer to that question was previously saved. It submits
  detected answers automatically, shows a checkmark in the web dashboard after
  the chat confirms each answer, and continues without requiring terminal
  input. CLI runs print a `[SUCCESS]` marker instead. It does not guess personal
  answers or use an AI provider.
- Saves recruiter email addresses from eligible job descriptions to
  `recruiter_emails.csv`; eligible external-apply jobs and detected
  destinations are saved to `external_apply_links.csv` and are not submitted
- Verifies each application actually went through before logging it as
  successful, rather than assuming success
- Logs every outcome (applied / skipped / stopped, with a reason) to
  `applications_log.csv`

## Requirements

- Python 3.10+
- A Naukri account with a resume already uploaded
- A residential IP (your own computer/phone/home network -- see the
  warning above)

## Setup

```bash
git clone <this-repo-url>
cd naukri-auto-apply
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### Run the Naukri CLI

Start the interactive menu from the project folder:

```bash
python cli.py
```

Choose one of the four workflows:

1. Capture a Naukri login session and then apply to perfect-match jobs; jobs
   that do not match are skipped and left visible.
2. Apply only to jobs with green checks for Early Applicant, Location, Work
   Experience, and Key Skills; non-matching jobs are skipped and left visible.
3. Only hide jobs that do not have green checks for all four match rows; do
   not submit applications.
4. Apply regardless of match score. The 24-hour posting-age check, existing
   application checks, and CAPTCHA/rate-limit stop conditions still apply.

Options 2, 3, and 4 require a saved Naukri session. Option 1 captures it before
applying. Apply modes continue through all discovered jobs in every
recommendation category; there is no application-count cap. Since there is
no profile configuration, the runner cannot filter by target role, company
preferences, or experience range. Screening answers are entered manually in
the visible browser.

### Run the multi-user web UI

Start the local profile dashboard from the project folder:

```bash
python web_ui.py
```

Open `http://127.0.0.1:5000`, create a username, and select its profile tile.
Each profile has a separate `user_data/<username>/` directory. That directory
stores the user's Naukri session and all workflow data, including application
logs, recruiter emails, external apply links, visited/rejected job records,
learned screening answers, missing-skill counts, and debug screenshots. For
example:

```
user_data/
  alice/
    session_naukri.json
    applications_log.csv
    recruiter_emails.csv
    learned_answers.json
    missing_skills.json
  bob/
    session_naukri.json
    applications_log.csv
    recruiter_emails.csv
    learned_answers.json
    missing_skills.json
```

Choose one of the four workflows shown on the profile page. For session
capture, log in in the opened browser; the session is saved automatically
after Naukri redirects to the authenticated homepage, the browser closes, and
the selected workflow continues. Existing files in the project root are left
untouched; new web UI runs use each selected user's directory and do not copy
shared historical records into profiles. The dashboard binds to localhost and
is not exposed to other devices on the network.

### Resume refresh (optional)

```bash
python naukri_refresh_resume.py
```

Removes and re-uploads your resume to keep your profile showing recent
activity. Can be scheduled (cron / Windows Task Scheduler) to run a few
times a day.

## Project structure

```
naukri_apply.py           Main search + apply loop
cli.py                    Interactive application entry point
naukri_refresh_resume.py  Resume remove/re-upload
login_capture.py          One-time manual login, saves session
schedule_daemon.py        Simple time-based scheduler (for environments
                           without reliable cron, e.g. Termux on Android)
mobile_dashboard.py       Optional Flask status dashboard, phone-friendly
common/
  learned_answers.py       Remembers answers you previously provided
  skill_tracker.py         Tallies key skills missing from matches
```

## Known limitations

- Naukri's page markup changes periodically; CSS selectors in
  `naukri_apply.py` may need updating when they do. If something stops
  working, compare against the real page's HTML (right-click the relevant
  element -> Inspect -> Copy outerHTML) and adjust the matching selector.
- LinkedIn support (`linkedin_apply.py`) exists but has had far less
  real-world testing than the Naukri path.
- Screening questions are not automatically answered from a profile; provide
  truthful answers in the visible browser when prompted.

## License

MIT -- use, modify, and share freely. No warranty; see the risk notice
above.
