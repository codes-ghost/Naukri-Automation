# Naukri Application Demo

Follow these steps from the project folder in Windows PowerShell. The first run requires a manual Naukri login; later runs reuse the saved browser session.

## 1. Prepare Python and Playwright

Python 3.10 or newer is required.

```powershell
py -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m playwright install chromium
```

These commands explicitly use the project virtual environment, so PowerShell activation is not required. If you see `ModuleNotFoundError`, reinstall the dependencies with the first command above and retry.

## 2. Review the built-in limits

The Naukri runner needs no `profile.yaml` or AI key. It processes all discovered jobs across all recommendation categories, waits 5 seconds between jobs, and scrolls up to 5 times per category. There is no application-count cap. Without personal profile settings, it cannot filter by target role, company preferences, or experience range. Perfect-match mode requires verified green checks for Early Applicant, Location, Work Experience, and Key Skills, plus a posting age of at most 24 hours.

## 3. Launch the CLI

Start the interactive menu:

```powershell
.\venv\Scripts\python.exe cli.py
```

Choose one of these actions:

1. **Login capture and start applying** opens the login browser, then starts applying to perfect-match jobs.
2. **Perfect match only** applies when Early Applicant, Location, Work Experience, and Key Skills all show green checks.
3. **Hide by match score only** checks each recommendation and hides jobs that fail or cannot verify any of those four checks; it does not apply.
4. **Apply regardless of match score** ignores match checks but still enforces the 24-hour posting-age check and other application safety checks.

Actions 2-4 require a valid saved session from `login_capture.py naukri`. Applying continues through all discovered jobs in every recommendation category; there is no application-count cap. Without profile settings, the runner cannot filter by target role, company preferences, or experience range.

For a screening question, enter or select your truthful answer in the visible Naukri browser, then press Enter in PowerShell. Answers are remembered and reused only when the same question appears again.

## 4. Review the output

- `applications_log.csv`: applied, skipped, uncertain, and stopped outcomes
- `recruiter_emails.csv`: recruiter emails found in eligible job descriptions, if any
- `external_apply_links.csv`: eligible jobs requiring an external application, if any; the script records the destination and does not submit on that external site
- `visited_recommended_jobs.json`: job IDs already visited, used to skip repeats
- `rejected_recommended_jobs.csv`: explicit suitability failures hidden in each category
- `learned_answers.json`: answers you supplied, reused only for matching questions

Check any `uncertain` application outcomes manually in Naukri. A CAPTCHA, login prompt, or rate-limit warning stops the run. Automation may violate Naukri's terms and can put your account at risk; use your own judgment and monitor the first run.
