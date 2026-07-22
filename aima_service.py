import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass

from playwright.sync_api import sync_playwright


API_BASE = "https://api-contactenos.aima.gov.pt/api/Form"
FORM_PAGE_URL = "https://contactenos.aima.gov.pt/contact-form"
LANGUAGE = "PT"
RECAPTCHA_SITE_KEY = "6LfdwdYqAAAAAHOU2VTdf5LqvHPuXz7cBf97xeoO"
RECAPTCHA_ACTION = "contact_form"
TOPIC_ID = "16"
SUBTOPIC_ID = "49"

PROFILE_DIR = os.getenv(
    "BROWSER_PROFILE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pw-profile"),
)

# A single persistent browser profile cannot safely be opened concurrently.
_BROWSER_LOCK = threading.Lock()


class AimaError(RuntimeError):
    pass


@dataclass(frozen=True)
class FormData:
    identity_type: str
    identity_number: str
    birth_date: str
    country_iso_code: str
    full_name: str
    email: str


def _parse_result(label: str, result: dict) -> dict:
    try:
        body = json.loads(result["body"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise AimaError(f"{label} returned an invalid response.") from error

    if not 200 <= result["status"] < 300 or body.get("success") is False:
        api_error = body.get("error") or {}
        message = api_error.get("message") if isinstance(api_error, dict) else None
        raise AimaError(message or f"{label} failed (HTTP {result['status']}).")

    return body


def _recaptcha_token(page) -> str:
    token = page.evaluate(
        """async ([siteKey, action]) => {
            await new Promise((resolve, reject) => {
                window.grecaptcha.enterprise.ready(resolve);
                setTimeout(
                    () => reject(new Error('reCAPTCHA ready timeout')),
                    15000
                );
            });
            return await window.grecaptcha.enterprise.execute(
                siteKey,
                { action }
            );
        }""",
        [RECAPTCHA_SITE_KEY, RECAPTCHA_ACTION],
    )
    if not token:
        raise AimaError("Could not obtain a reCAPTCHA token.")
    return token


def _post_json(page, url: str, payload: dict, headers: dict) -> dict:
    return page.evaluate(
        """async ([url, payload, headers]) => {
            const response = await fetch(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json', ...headers},
                body: JSON.stringify(payload),
                credentials: 'include',
            });
            return {
                status: response.status,
                body: await response.text()
            };
        }""",
        [url, payload, headers],
    )


def _post_form(page, url: str, fields: dict, headers: dict) -> dict:
    return page.evaluate(
        """async ([url, fields, headers]) => {
            const form = new FormData();
            for (const [key, value] of Object.entries(fields)) {
                form.append(key, value);
            }
            const response = await fetch(url, {
                method: 'POST',
                headers,
                body: form,
                credentials: 'include',
            });
            return {
                status: response.status,
                body: await response.text()
            };
        }""",
        [url, fields, headers],
    )


@contextmanager
def _browser_page():
    with _BROWSER_LOCK:
        with sync_playwright() as playwright:
            launch_options = {
                "headless": False,
                "locale": "pt-PT",
                "viewport": {"width": 1280, "height": 800},
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            try:
                context = playwright.chromium.launch_persistent_context(
                    PROFILE_DIR,
                    channel="chrome",
                    **launch_options,
                )
            except Exception:
                context = playwright.chromium.launch_persistent_context(
                    PROFILE_DIR,
                    **launch_options,
                )

            try:
                context.add_init_script(
                    "Object.defineProperty("
                    "navigator, 'webdriver', {get: () => undefined}"
                    ");"
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(FORM_PAGE_URL, wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => window.grecaptcha && window.grecaptcha.enterprise",
                    timeout=30000,
                )
                page.mouse.move(200, 200)
                page.mouse.move(600, 400)
                page.wait_for_timeout(1500)
                yield page
            finally:
                context.close()


def request_mfa(email: str) -> None:
    with _browser_page() as page:
        result = _post_json(
            page,
            f"{API_BASE}/generate-mfa-code?language={LANGUAGE}",
            {"email": email},
            {"Recaptcha-Token": _recaptcha_token(page)},
        )
        _parse_result("MFA request", result)


def submit_form(form: FormData, mfa_code: str) -> None:
    identity_fields = {
        "passport": "passportNumber",
        "residence": "residencePermitNumber",
    }
    try:
        identity_api_field = identity_fields[form.identity_type]
    except KeyError as error:
        raise AimaError("Unsupported identity type.") from error

    with _browser_page() as page:
        validation_result = _post_json(
            page,
            f"{API_BASE}/validate-mfa-code",
            {"email": form.email, "mfaCode": mfa_code},
            {},
        )
        validation = _parse_result("MFA validation", validation_result)
        auth_token = validation.get("result")
        if not auth_token:
            raise AimaError("MFA validation did not return an auth token.")

        fields = {
            "authorized": "true",
            identity_api_field: form.identity_number,
            "birthDate": form.birth_date,
            "countryISOCode": form.country_iso_code.upper(),
            "email": form.email,
            "name": form.full_name,
            "topicId": TOPIC_ID,
            "subtopicId": SUBTOPIC_ID,
            "mfaCode": json.dumps(list(mfa_code)),
        }
        submit_result = _post_form(
            page,
            f"{API_BASE}?language={LANGUAGE}",
            fields,
            {
                "Authorization": f"Bearer {auth_token}",
                "Recaptcha-Token": _recaptcha_token(page),
            },
        )
        _parse_result("Form submission", submit_result)
