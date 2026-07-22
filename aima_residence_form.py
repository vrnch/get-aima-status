#!/usr/bin/env python3

import json
import os
import sys

from playwright.sync_api import sync_playwright


API_BASE = "https://api-contactenos.aima.gov.pt/api/Form"
FORM_PAGE_URL = "https://contactenos.aima.gov.pt/contact-form"
LANGUAGE = "PT"
RECAPTCHA_SITE_KEY = "6LfdwdYqAAAAAHOU2VTdf5LqvHPuXz7cBf97xeoO"
RECAPTCHA_ACTION = "contact_form"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "aima_residence_config.json")

# A persistent browser profile lets the reCAPTCHA _GRECAPTCHA cookie build up
# genuine reputation across runs, which raises the Enterprise score.
PROFILE_DIR = os.path.join(SCRIPT_DIR, ".pw-profile")

TOPIC_ID = "16"
SUBTOPIC_ID = "49"


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError:
        sys.exit(
            "Missing aima_residence_config.json. Copy "
            "aima_residence_config.example.json to aima_residence_config.json "
            "and enter your details."
        )
    except json.JSONDecodeError as error:
        sys.exit(f"Invalid JSON in aima_residence_config.json: {error}")

    required_fields = (
        "residence_permit_number",
        "birth_date",
        "country_iso_code",
        "full_name",
        "email",
    )
    missing = [field for field in required_fields if not config.get(field)]
    if missing:
        sys.exit(f"Missing config fields: {', '.join(missing)}")
    if "@" not in config["email"]:
        sys.exit("Invalid email in aima_residence_config.json.")

    return config


def show_result(label: str, result: dict) -> None:
    print(f"\n{label}: HTTP {result['status']}")
    body = result["body"]
    try:
        print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
    except (ValueError, TypeError):
        print(body)


def recaptcha_token(page, action: str) -> str:
    return page.evaluate(
        """async ([siteKey, action]) => {
            await new Promise((resolve, reject) => {
                window.grecaptcha.enterprise.ready(() => resolve());
                setTimeout(() => reject(new Error('reCAPTCHA ready timeout')), 15000);
            });
            return await window.grecaptcha.enterprise.execute(siteKey, { action });
        }""",
        [RECAPTCHA_SITE_KEY, action],
    )


def post_json(page, url, payload, headers):
    return page.evaluate(
        """async ([url, payload, headers]) => {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...headers },
                body: JSON.stringify(payload),
                credentials: 'include',
            });
            return { status: res.status, body: await res.text() };
        }""",
        [url, payload, headers],
    )


def post_form(page, url, fields, headers):
    return page.evaluate(
        """async ([url, fields, headers]) => {
            const fd = new FormData();
            for (const [key, value] of Object.entries(fields)) {
                fd.append(key, value);
            }
            const res = await fetch(url, {
                method: 'POST',
                headers: headers,
                body: fd,
                credentials: 'include',
            });
            return { status: res.status, body: await res.text() };
        }""",
        [url, fields, headers],
    )


def main() -> None:
    config = load_config()
    email = config["email"]

    with sync_playwright() as playwright:
        launch_args = {
            "headless": False,
            "locale": "pt-PT",
            "viewport": {"width": 1280, "height": 800},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        try:
            context = playwright.chromium.launch_persistent_context(
                PROFILE_DIR, channel="chrome", **launch_args
            )
        except Exception:
            context = playwright.chromium.launch_persistent_context(
                PROFILE_DIR, **launch_args
            )

        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(FORM_PAGE_URL, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => window.grecaptcha && window.grecaptcha.enterprise",
            timeout=30000,
        )
        # Small warm-up so reCAPTCHA observes real activity before scoring.
        page.mouse.move(200, 200)
        page.mouse.move(600, 400)
        page.wait_for_timeout(1500)

        # 1. Request the MFA code (browser context, with a fresh token).
        generate = post_json(
            page,
            f"{API_BASE}/generate-mfa-code?language={LANGUAGE}",
            {"email": email},
            {"Recaptcha-Token": recaptcha_token(page, RECAPTCHA_ACTION)},
        )
        show_result("MFA request", generate)
        if generate["status"] != 200:
            context.close()
            sys.exit("Failed to request the MFA code.")

        print("\nCheck your email for the six-digit MFA code.")
        mfa_code = input("MFA code: ").strip()
        if not mfa_code.isdigit() or len(mfa_code) != 6:
            context.close()
            sys.exit("The MFA code must contain exactly six digits.")

        # 2. Validate the MFA code.
        validate = post_json(
            page,
            f"{API_BASE}/validate-mfa-code",
            {"email": email, "mfaCode": mfa_code},
            {},
        )
        show_result("MFA validation", validate)
        if validate["status"] != 200:
            context.close()
            sys.exit("MFA validation failed.")

        auth_token = json.loads(validate["body"]).get("result")
        if not auth_token:
            context.close()
            sys.exit("MFA validation did not return an auth token.")

        # 3. Submit the form (new token for the submit action).
        submit = post_form(
            page,
            f"{API_BASE}?language={LANGUAGE}",
            {
                "authorized": "true",
                "residencePermitNumber": config["residence_permit_number"],
                "birthDate": config["birth_date"],
                "countryISOCode": config["country_iso_code"].upper(),
                "email": email,
                "name": config["full_name"],
                "topicId": TOPIC_ID,
                "subtopicId": SUBTOPIC_ID,
                "mfaCode": json.dumps(list(mfa_code)),
            },
            {
                "Authorization": f"Bearer {auth_token}",
                "Recaptcha-Token": recaptcha_token(page, RECAPTCHA_ACTION),
            },
        )
        show_result("Form submission", submit)
        context.close()

        if submit["status"] != 200:
            sys.exit("\nThe API rejected the form submission.")

    print("\nForm submitted successfully.")


if __name__ == "__main__":
    main()
