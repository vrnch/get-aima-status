#!/usr/bin/env python3
"""One-time local run: passport status request via AIMA contact form."""

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from aima_service import (
    AimaError,
    FormData,
    request_mfa,
    start_browser,
    stop_browser,
    submit_form,
)

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "aima_config.json"

load_dotenv(SCRIPT_DIR / ".env", override=True)


def load_config() -> dict:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(
            "Missing aima_config.json. Copy aima_config.example.json to "
            "aima_config.json and enter your details."
        )
    except json.JSONDecodeError as error:
        sys.exit(f"Invalid JSON in aima_config.json: {error}")

    required_fields = (
        "passport_number",
        "birth_date",
        "country_iso_code",
        "full_name",
        "email",
    )
    missing = [field for field in required_fields if not config.get(field)]
    if missing:
        sys.exit(f"Missing config fields: {', '.join(missing)}")
    if "@" not in config["email"]:
        sys.exit("Invalid email in aima_config.json.")
    return config


def main() -> None:
    config = load_config()
    start_browser()
    try:
        print("Requesting MFA code…")
        request_mfa(config["email"])
        print("Check your email for the six-digit MFA code.")
        mfa_code = input("MFA code: ").strip()
        if not mfa_code.isdigit() or len(mfa_code) != 6:
            sys.exit("The MFA code must contain exactly six digits.")

        print("Submitting…")
        tracking = submit_form(
            FormData(
                identity_type="passport",
                identity_number=config["passport_number"],
                birth_date=config["birth_date"],
                country_iso_code=config["country_iso_code"],
                full_name=config["full_name"],
                email=config["email"],
            ),
            mfa_code,
        )
    except AimaError as error:
        sys.exit(f"\nFailed: {error}")
    finally:
        stop_browser()

    print("\nForm submitted successfully.")
    if tracking:
        print(f"Tracking: https://contactenos.aima.gov.pt/tracking/{tracking}")


if __name__ == "__main__":
    main()
