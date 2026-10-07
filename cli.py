"""Interactive command-line entry point for the Naukri application workflow."""
from login_capture import capture_session
from naukri_apply import run


def main() -> None:
    print("\nNaukri Job Application")
    print("1. Login capture and start applying (perfect match only)")
    print("2. Apply only to perfect-match jobs")
    print("3. Only hide jobs that are not a perfect match")
    print("4. Apply to jobs regardless of match score")
    print("0. Exit")

    choice = input("Choose an option: ").strip()
    if choice == "1":
        capture_session("naukri")
        run(match_score_mode="perfect")
    elif choice == "2":
        run(match_score_mode="perfect")
    elif choice == "3":
        run(hide_by_match_score=True, hide_rejected=False)
    elif choice == "4":
        run(match_score_mode="any", hide_rejected=False)
    elif choice == "0":
        print("Exiting.")
    else:
        raise SystemExit("Invalid option. Run cli.py and choose 0, 1, 2, 3, or 4.")


if __name__ == "__main__":
    main()
