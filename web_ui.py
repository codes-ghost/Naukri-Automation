"""Local web interface for selecting user profiles and starting Naukri workflows."""
from __future__ import annotations

import logging
import os
import re
import secrets
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

ROOT_DIR = Path(__file__).resolve().parent
USER_DATA_DIR = ROOT_DIR / "user_data"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
WORKFLOW_NAMES = {
    "1": "Capture login and apply to perfect-match jobs",
    "2": "Apply only to perfect-match jobs",
    "3": "Hide jobs that are not a perfect match",
    "4": "Apply regardless of match score",
}

app = Flask(__name__)
app.secret_key = os.environ.get("NAUKRI_UI_SECRET", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_job_lock = threading.Lock()
_job_state: dict[str, Any] = {
    "status": "idle",
    "username": None,
    "option": None,
    "message": "Choose a profile to begin.",
    "output": [],
}


def _username_key(username: str) -> str:
    value = username.strip()
    if not USERNAME_PATTERN.fullmatch(value):
        raise ValueError("Use 1–32 letters, numbers, underscores, or hyphens; start with a letter or number.")
    return value.casefold()


def _profile_directory(username: str) -> Path:
    key = _username_key(username)
    directory = (USER_DATA_DIR / key).resolve()
    if directory.parent != USER_DATA_DIR.resolve():
        raise ValueError("Invalid profile name.")
    return directory


def _session_path(profile_dir: Path) -> Path:
    return profile_dir / "session_naukri.json"


def _profiles() -> list[dict[str, Any]]:
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    profiles = []
    for directory in USER_DATA_DIR.iterdir():
        if directory.is_dir() and USERNAME_PATTERN.fullmatch(directory.name):
            profiles.append({
                "username": directory.name,
                "has_session": _session_path(directory).is_file(),
            })
    return sorted(profiles, key=lambda profile: profile["username"].casefold())


def _start_workflow(username: str, option: str) -> None:
    global _job_state
    profile_dir = _profile_directory(username)
    profile_dir.mkdir(parents=True, exist_ok=True)
    session_path = _session_path(profile_dir)

    with _job_lock:
        if _job_state["status"] in {"queued", "running"}:
            raise RuntimeError("A workflow is already running. Wait for it to finish before starting another.")
        _job_state = {
            "status": "queued",
            "username": username,
            "option": option,
            "message": "Starting workflow…",
            "output": [],
        }

    worker = threading.Thread(
        target=_run_workflow,
        args=(username, option, profile_dir, session_path),
        daemon=True,
    )
    worker.start()


def _run_workflow(username: str, option: str, profile_dir: Path, session_path: Path) -> None:
    global _job_state
    environment = os.environ.copy()
    environment["NAUKRI_DATA_DIR"] = str(profile_dir)
    command = [
        sys.executable,
        "-u",
        str(ROOT_DIR / "cli.py"),
        "--option",
        option,
        "--session-file",
        str(session_path),
    ]
    with _job_lock:
        _job_state["status"] = "running"
        _job_state["message"] = (
            "Log in in the opened browser. The session will save automatically, then the workflow will continue."
            if option == "1"
            else "Workflow running in the browser."
        )

    try:
        process = subprocess.Popen(
            command,
            cwd=profile_dir,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            with _job_lock:
                _job_state["output"].append(line.rstrip())
                _job_state["output"] = _job_state["output"][-300:]
        return_code = process.wait()
        with _job_lock:
            _job_state["status"] = "completed" if return_code == 0 else "failed"
            _job_state["message"] = (
                "Workflow completed."
                if return_code == 0
                else f"Workflow exited with code {return_code}."
            )
    except OSError as exc:
        logger.exception("Could not start workflow for profile %s", username)
        with _job_lock:
            _job_state["status"] = "failed"
            _job_state["message"] = f"Could not start workflow: {exc}"
            _job_state["output"].append(str(exc))


@app.get("/")
def home():
    return render_template("home.html", profiles=_profiles())


@app.post("/users")
def create_profile():
    username = request.form.get("username", "")
    try:
        key = _username_key(username)
        _profile_directory(key).mkdir(parents=True, exist_ok=True)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("home"))
    return redirect(url_for("profile", username=key))


@app.get("/users/<username>")
def profile(username: str):
    try:
        profile_dir = _profile_directory(username)
    except ValueError:
        abort(404)
    if not profile_dir.is_dir():
        abort(404)
    with _job_lock:
        current_job = dict(_job_state) if _job_state["username"] == username.casefold() else None
    return render_template(
        "profile.html",
        username=username.casefold(),
        has_session=_session_path(profile_dir).is_file(),
        workflows=WORKFLOW_NAMES,
        current_job=current_job,
    )


@app.post("/users/<username>/run")
def start_workflow(username: str):
    try:
        profile_dir = _profile_directory(username)
    except ValueError:
        abort(404)
    if not profile_dir.is_dir():
        abort(404)

    option = request.form.get("option", "")
    if option not in WORKFLOW_NAMES:
        abort(400)
    if option != "1" and not _session_path(profile_dir).is_file():
        flash("Capture a Naukri login session before starting this workflow.", "error")
        return redirect(url_for("profile", username=username.casefold()))
    try:
        _start_workflow(username.casefold(), option)
    except RuntimeError as exc:
        flash(str(exc), "error")
    return redirect(url_for("profile", username=username.casefold()))


@app.get("/api/users/<username>/job")
def workflow_status(username: str):
    try:
        _profile_directory(username)
    except ValueError:
        abort(404)
    with _job_lock:
        if _job_state["username"] != username.casefold():
            return jsonify(status="idle", message="No workflow has been started for this profile.", output=[])
        return jsonify(_job_state)


if __name__ == "__main__":
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
