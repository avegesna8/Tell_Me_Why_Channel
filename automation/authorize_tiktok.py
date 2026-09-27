"""Authorize the owner TikTok account and save renewable API credentials locally."""
from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import dotenv_values, set_key


AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
DEFAULT_SCOPES = ("user.info.basic", "video.publish")
DEFAULT_STATE_FILE = Path(".tiktok-oauth-state.json")


def _required(values: dict[str, str | None], name: str) -> str:
    value = (values.get(name) or "").strip()
    if not value:
        raise ValueError(f"Set {name} in .env before running this command.")
    return value


def begin(values: dict[str, str | None], state_file: Path = DEFAULT_STATE_FILE) -> str:
    if state_file.exists():
        raise ValueError(f"{state_file} already exists; finish or delete the previous authorization attempt.")
    state = secrets.token_urlsafe(32)
    state_file.write_text(json.dumps({"state": state}), encoding="utf-8")
    state_file.chmod(0o600)
    query = urlencode({
        "client_key": _required(values, "TIKTOK_CLIENT_KEY"),
        "scope": ",".join(DEFAULT_SCOPES),
        "response_type": "code",
        "redirect_uri": _required(values, "TIKTOK_REDIRECT_URI"),
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{query}"


def exchange(
    values: dict[str, str | None],
    result_file: Path,
    state_file: Path = DEFAULT_STATE_FILE,
    session=requests,
) -> dict[str, str]:
    if not state_file.is_file():
        raise ValueError("No pending TikTok authorization was found. Run the start command first.")
    expected_state = json.loads(state_file.read_text(encoding="utf-8")).get("state")
    result = json.loads(result_file.read_text(encoding="utf-8"))
    if result.get("error"):
        raise RuntimeError(f"TikTok authorization failed: {result.get('error_description') or result['error']}")
    if not expected_state or not secrets.compare_digest(str(result.get("state", "")), str(expected_state)):
        raise RuntimeError("TikTok authorization state did not match; start again.")
    code = str(result.get("code", "")).strip()
    if not code:
        raise RuntimeError("TikTok did not return an authorization code.")

    response = session.post(
        TOKEN_URL,
        data={
            "client_key": _required(values, "TIKTOK_CLIENT_KEY"),
            "client_secret": _required(values, "TIKTOK_CLIENT_SECRET"),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": _required(values, "TIKTOK_REDIRECT_URI"),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("TikTok token exchange returned an unreadable response.") from exc
    if not response.ok or payload.get("error"):
        error = payload.get("error", {})
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise RuntimeError(f"TikTok token exchange failed: {message or f'HTTP {response.status_code}'}")

    credentials = {
        "TIKTOK_ACCESS_TOKEN": _required(payload, "access_token"),
        "TIKTOK_REFRESH_TOKEN": _required(payload, "refresh_token"),
        "TIKTOK_OPEN_ID": _required(payload, "open_id"),
    }
    returned_scopes = {scope.strip() for scope in str(payload.get("scope", "")).split(",") if scope.strip()}
    missing = set(DEFAULT_SCOPES) - returned_scopes
    if missing:
        raise RuntimeError(f"TikTok did not grant required scopes: {', '.join(sorted(missing))}")
    state_file.unlink()
    return credentials


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start")
    finish = subparsers.add_parser("finish")
    finish.add_argument("--result-file", required=True, type=Path)
    args = parser.parse_args()

    if not args.env_file.is_file():
        raise ValueError(f"Environment file not found: {args.env_file}")
    values = {**os.environ, **dotenv_values(args.env_file)}
    if args.command == "start":
        print("Open this private authorization URL in your browser:")
        print(begin(values, args.state_file))
        return

    credentials = exchange(values, args.result_file, args.state_file)
    for key, value in credentials.items():
        set_key(str(args.env_file), key, value, quote_mode="never")
    args.env_file.chmod(0o600)
    print("TikTok authorization succeeded.")
    print("Access token, refresh token, and account ID were saved to .env and were not displayed.")


if __name__ == "__main__":
    main()
