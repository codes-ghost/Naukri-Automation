"""Interactive command-line entry point for the Naukri application workflow."""
import argparse
import os
from pathlib import Path

from login_capture import capture_session


DEFAULT_SESSION_FILE = Path("session_naukri.json")


def run_option(choice: str, session_file: str | Path = DEFAULT_SESSION_FILE) -> None:
    """Run one of the interactive menu's workflows with the selected session."""
    session_path = Path(session_file).expanduser().resolve()
    session_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["NAUKRI_DATA_DIR"] = str(session_path.parent)
    from naukri_apply import run

    if choice == "1":
        capture_session("naukri", output_path=session_path)
        run(match_score_mode="perfect", hide_rejected=False, session_file=session_path)
    elif choice == "2":
        run(match_score_mode="perfect", hide_rejected=False, session_file=session_path)
    elif choice == "3":
        run(hide_by_match_score=True, hide_rejected=False, session_file=session_path)
    elif choice == "4":
        run(match_score_mode="any", hide_rejected=False, session_file=session_path)
    else:
        raise ValueError("Invalid option. Choose 1, 2, 3, or 4.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Naukri application workflow.")
    parser.add_argument(
        "--option",
        choices=("1", "2", "3", "4"),
        help="run a menu workflow without prompting for a menu choice",
    )
    parser.add_argument(
        "--session-file",
        type=Path,
        default=DEFAULT_SESSION_FILE,
        help="path to the Naukri browser session file",
    )
    args = parser.parse_args()

    if args.option:
        run_option(args.option, args.session_file)
        return
    if args.session_file != DEFAULT_SESSION_FILE:
        parser.error("--session-file can only be used together with --option")

    print("\nNaukri Job Application")
    print("1. Login capture and start applying (perfect match only)")
    print("2. Apply only to perfect-match jobs")
    print("3. Only hide jobs that are not a perfect match")
    print("4. Apply to jobs regardless of match score")
    print("0. Exit")

    choice = input("Choose an option: ").strip()
    if choice == "0":
        print("Exiting.")
        return
    try:
        run_option(choice)
    except ValueError as exc:
        raise SystemExit(f"{exc} Run cli.py and choose 0, 1, 2, 3, or 4.") from exc


if __name__ == "__main__":
    main()
