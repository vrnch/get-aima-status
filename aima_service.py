import concurrent.futures
import json
import os
import queue
import threading
from dataclasses import dataclass
from typing import Callable, Optional

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


def _format_api_error(body: dict, status: int, label: str) -> str:
    api_error = body.get("error")
    parts = []

    if isinstance(api_error, dict):
        message = api_error.get("message")
        details = api_error.get("details")
        if message:
            parts.append(str(message))
        if details is None:
            pass
        elif isinstance(details, str) and details.strip():
            parts.append(details.strip())
        elif isinstance(details, list):
            for item in details:
                if isinstance(item, dict):
                    field = item.get("field") or item.get("property") or item.get("key")
                    text = (
                        item.get("message")
                        or item.get("error")
                        or item.get("description")
                        or json.dumps(item, ensure_ascii=False)
                    )
                    parts.append(f"- {field}: {text}" if field else f"- {text}")
                else:
                    parts.append(f"- {item}")
        elif isinstance(details, dict):
            for key, value in details.items():
                if isinstance(value, list):
                    for item in value:
                        parts.append(f"- {key}: {item}")
                else:
                    parts.append(f"- {key}: {value}")
    elif api_error:
        parts.append(str(api_error))

    if not parts:
        parts.append(f"{label} failed (HTTP {status}).")
        raw = json.dumps(body, ensure_ascii=False)
        if len(raw) < 1500:
            parts.append(raw)

    unique = []
    for part in parts:
        if part not in unique:
            unique.append(part)
    return "\n".join(unique)


def _parse_result(label: str, result: dict) -> dict:
    try:
        body = json.loads(result["body"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise AimaError(f"{label} returned an invalid response.") from error

    if not 200 <= result["status"] < 300 or body.get("success") is False:
        message = _format_api_error(body, result["status"], label)
        print(f"{label} error payload: {json.dumps(body, ensure_ascii=False)}")
        raise AimaError(message)

    return body


def _is_recaptcha_failure(message: str) -> bool:
    return "recaptcha" in message.lower()


def _ensure_form_page(page, *, force_reload: bool = False) -> None:
    needs_load = force_reload
    if not needs_load:
        try:
            current = page.url or ""
            has_recaptcha = page.evaluate(
                "() => !!(window.grecaptcha && window.grecaptcha.enterprise)"
            )
            needs_load = (
                "contactenos.aima.gov.pt" not in current or not has_recaptcha
            )
        except Exception:
            needs_load = True

    if needs_load:
        page.goto(FORM_PAGE_URL, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => window.grecaptcha && window.grecaptcha.enterprise",
            timeout=30000,
        )


def _recaptcha_token(page, *, fresh: bool = False) -> str:
    _ensure_form_page(page, force_reload=fresh)
    page.mouse.move(180, 220)
    page.mouse.move(640, 420)
    page.mouse.wheel(0, 350)
    page.wait_for_timeout(3000)
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
                headers: {
                    'Content-Type': 'application/json',
                    'Accept-Language': 'PT',
                    ...headers,
                },
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
                headers: {'Accept-Language': 'PT', ...headers},
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


class _BrowserWorker:
    """Runs all Playwright calls on one dedicated thread."""

    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop,
            name="aima-browser-worker",
            daemon=True,
        )
        self._started = threading.Event()
        self._start_error: Optional[BaseException] = None

    def start(self) -> None:
        if self._thread.is_alive():
            return
        self._thread.start()
        if not self._started.wait(timeout=120):
            raise AimaError("Timed out while starting the browser.")
        if self._start_error is not None:
            raise AimaError(f"Browser failed to start: {self._start_error}")

    def stop(self) -> None:
        if not self._thread.is_alive():
            return
        self._jobs.put(None)
        self._thread.join(timeout=30)

    def call(self, func: Callable, *args, timeout: float = 180):
        if not self._thread.is_alive():
            raise AimaError("Browser worker is not running. Call start_browser().")
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((func, args, future))
        return future.result(timeout=timeout)

    def _loop(self) -> None:
        try:
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

                context.add_init_script(
                    "Object.defineProperty("
                    "navigator, 'webdriver', {get: () => undefined}"
                    ");"
                )
                page = context.pages[0] if context.pages else context.new_page()
                _ensure_form_page(page)
                page.mouse.move(200, 200)
                page.mouse.move(600, 400)
                page.wait_for_timeout(2000)
                self._started.set()

                try:
                    while True:
                        job = self._jobs.get()
                        if job is None:
                            break
                        func, args, future = job
                        try:
                            if page.is_closed():
                                page = context.new_page()
                                _ensure_form_page(page)
                            else:
                                _ensure_form_page(page)
                            future.set_result(func(page, *args))
                        except BaseException as error:
                            future.set_exception(error)
                finally:
                    context.close()
        except BaseException as error:
            self._start_error = error
            self._started.set()


_WORKER = _BrowserWorker()


def start_browser() -> None:
    _WORKER.start()


def stop_browser() -> None:
    _WORKER.stop()


def _identity_api_field(identity_type: str) -> str:
    identity_fields = {
        "passport": "passportNumber",
        "residence": "residencePermitNumber",
    }
    try:
        return identity_fields[identity_type]
    except KeyError as error:
        raise AimaError("Unsupported identity type.") from error


def _request_mfa_on_page(page, email: str) -> None:
    result = _post_json(
        page,
        f"{API_BASE}/generate-mfa-code?language={LANGUAGE}",
        {"email": email},
        {"Recaptcha-Token": _recaptcha_token(page, fresh=True)},
    )
    _parse_result("MFA request", result)


def _submit_form_on_page(page, form: FormData, mfa_code: str) -> Optional[str]:
    validation = _parse_result(
        "MFA validation",
        _post_json(
            page,
            f"{API_BASE}/validate-mfa-code",
            {"email": form.email, "mfaCode": mfa_code},
            {},
        ),
    )
    auth_token = validation.get("result")
    if not auth_token:
        raise AimaError("MFA validation did not return an auth token.")

    fields = {
        "authorized": "true",
        _identity_api_field(form.identity_type): form.identity_number,
        "birthDate": form.birth_date,
        "countryISOCode": form.country_iso_code.upper(),
        "email": form.email,
        "name": form.full_name,
        "topicId": TOPIC_ID,
        "subtopicId": SUBTOPIC_ID,
        "mfaCode": json.dumps(list(mfa_code)),
    }

    last_error: Optional[AimaError] = None
    for attempt in range(3):
        try:
            body = _parse_result(
                "Form submission",
                _post_form(
                    page,
                    f"{API_BASE}?language={LANGUAGE}",
                    fields,
                    {
                        "Authorization": f"Bearer {auth_token}",
                        "Recaptcha-Token": _recaptcha_token(page, fresh=True),
                    },
                ),
            )
            try:
                return body["result"]["result"]["trackingToken"]
            except (KeyError, TypeError):
                return None
        except AimaError as error:
            last_error = error
            if not _is_recaptcha_failure(str(error)) or attempt == 2:
                raise
            print(f"reCAPTCHA rejected submit attempt {attempt + 1}/3; retrying…")
            page.wait_for_timeout(2500 + attempt * 2000)

    assert last_error is not None
    raise last_error


def request_mfa(email: str) -> None:
    _WORKER.call(_request_mfa_on_page, email)


def submit_form(form: FormData, mfa_code: str) -> Optional[str]:
    return _WORKER.call(_submit_form_on_page, form, mfa_code)
