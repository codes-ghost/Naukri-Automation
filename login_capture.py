"""
Run this once per site (Naukri, LinkedIn) before running the apply scripts.

It opens a real, visible Chromium window. YOU log in by hand — type your
password, complete 2FA, solve any CAPTCHA yourself. After Naukri redirects to
your authenticated homepage, the script automatically saves your session
(cookies + local storage) and closes the browser.

Usage:
    python login_capture.py naukri
    python login_capture.py linkedin
"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
from common.data_paths import data_path

SITES = {
    "naukri": "https://www.naukri.com/nlogin/login",
    "linkedin": "https://www.linkedin.com/login",
}
AUTHENTICATED_URL_PATTERNS = {
    "naukri": "**/mnjuser/**",
    "linkedin": "**/feed/**",
}


def capture_session(site: str, output_path: str | Path | None = None) -> str:
    """Save the session after a successful login redirect, then close the browser."""
    if site not in SITES:
        raise ValueError(f"Unknown site '{site}'. Choose one of: {', '.join(SITES)}")
    url = SITES[site]
    out_path = data_path(output_path) if output_path else data_path(f"session_{site}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url)

        print(f"\nA browser window is open at {url}")
        print("Log in by hand: password, 2FA, any CAPTCHA — all of it.")
        print("The session will be saved and the browser closed after login succeeds.")
        page.wait_for_url(AUTHENTICATED_URL_PATTERNS[site], timeout=0)

        context.storage_state(path=str(out_path))
        print(f"Session saved to {out_path}. Keep this file private — it's equivalent to being logged in.")
        browser.close()
    return str(out_path)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in SITES:
        print(f"Usage: python login_capture.py [{'|'.join(SITES)}]")
        sys.exit(1)
    capture_session(sys.argv[1])


if __name__ == "__main__":
    main()
