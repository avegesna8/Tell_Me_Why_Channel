"""Exchange a temporary Meta token and save durable publishing credentials locally."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import requests
from dotenv import dotenv_values, set_key


GRAPH_ROOT = "https://graph.facebook.com/v26.0"


def _required(values: dict[str, str | None], name: str) -> str:
    value = (values.get(name) or "").strip()
    if not value:
        raise ValueError(f"Set {name} in .env before running this command.")
    return value


def _json(response: requests.Response, label: str) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Meta {label} returned an unreadable response.") from exc
    if not response.ok or "error" in payload:
        error = payload.get("error", {})
        message = error.get("message", f"HTTP {response.status_code}")
        raise RuntimeError(f"Meta {label} failed: {message}")
    return payload


def exchange_credentials(values: dict[str, str | None], session=requests) -> dict[str, str]:
    app_id = _required(values, "META_APP_ID")
    app_secret = _required(values, "META_APP_SECRET")
    temporary_token = _required(values, "META_USER_ACCESS_TOKEN")
    page_id = _required(values, "META_PAGE_ID")

    exchanged = session.get(
        f"{GRAPH_ROOT}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": temporary_token,
        },
        timeout=30,
    )
    long_user_token = _required(_json(exchanged, "token exchange"), "access_token")

    page_response = session.get(
        f"{GRAPH_ROOT}/{page_id}",
        params={"fields": "id,name,access_token,instagram_business_account{id,username}"},
        headers={"Authorization": f"Bearer {long_user_token}"},
        timeout=30,
    )
    page = _json(page_response, "Page lookup")
    if str(page.get("id", "")) != page_id:
        raise RuntimeError("Meta returned a different Page than META_PAGE_ID.")
    page_token = _required(page, "access_token")
    instagram = page.get("instagram_business_account") or {}
    instagram_id = str(instagram.get("id", "")).strip()
    expected_instagram_id = (values.get("META_INSTAGRAM_ACCOUNT_ID") or "").strip()
    if expected_instagram_id and instagram_id != expected_instagram_id:
        raise RuntimeError("The Page is connected to a different Instagram account than configured.")
    if not instagram_id:
        raise RuntimeError("The Facebook Page has no connected professional Instagram account.")

    return {
        "META_USER_ACCESS_TOKEN": long_user_token,
        "META_PAGE_ACCESS_TOKEN": page_token,
        "META_INSTAGRAM_ACCOUNT_ID": instagram_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    if not args.env_file.is_file():
        raise ValueError(f"Environment file not found: {args.env_file}")

    values = {**os.environ, **dotenv_values(args.env_file)}
    credentials = exchange_credentials(values)
    for key, value in credentials.items():
        set_key(str(args.env_file), key, value, quote_mode="never")
    args.env_file.chmod(0o600)
    print("Meta authorization succeeded.")
    print(f"Facebook Page ID: {_required(values, 'META_PAGE_ID')}")
    print(f"Instagram account ID: {credentials['META_INSTAGRAM_ACCOUNT_ID']}")
    print("Long-lived user and Page tokens were saved to .env and were not displayed.")


if __name__ == "__main__":
    main()
