"""
Run this once per site (Naukri, LinkedIn) before running the apply scripts.

It opens a real, visible Chromium window. YOU log in by hand — type your
password, complete 2FA, solve any CAPTCHA yourself. Once you're on the
logged-in homepage, come back to this terminal and press Enter. The script
then saves your session (cookies + local storage) to a local file so the
apply scripts can reuse it without ever seeing your password.

Usage:
    python login_capture.py naukri
    python login_capture.py linkedin
"""
import sys
from playwright.sync_api import sync_playwright

SITES = {
    "naukri": "https://www.naukri.com/nlogin/login",
    "linkedin": "https://www.linkedin.com/login",
}


def capture_session(site: str) -> str:
    """Opens a real browser so you can log in by hand, then saves the session
    (cookies + local storage) to session_<site>.json and returns that path."""
    if site not in SITES:
        raise ValueError(f"Unknown site '{site}'. Choose one of: {', '.join(SITES)}")
    url = SITES[site]
    out_path = f"session_{site}.json"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url)

        print(f"\nA browser window is open at {url}")
        print("Log in by hand: password, 2FA, any CAPTCHA — all of it.")
        input("Once you're on the logged-in homepage, press Enter here to save the session... ")

        context.storage_state(path=out_path)
        print(f"Session saved to {out_path}. Keep this file private — it's equivalent to being logged in.")
        browser.close()
    return out_path


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in SITES:
        print(f"Usage: python login_capture.py [{'|'.join(SITES)}]")
        sys.exit(1)
    capture_session(sys.argv[1])


if __name__ == "__main__":
    main()
