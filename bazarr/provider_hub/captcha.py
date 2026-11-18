# coding=utf-8
"""The captcha solver the host offers plugins, over the worker's own pipes.

A plugin asks through ``bazarr_host.solve_recaptcha`` and the call reaches
``solve_call`` here (see ``worker.py``). The vendor and its credentials are the
user's settings; they never enter the worker, and the answer is the token alone.

Failures reach the plugin as short, fixed messages. What the vendor said, which can
carry an account detail, goes to the host log only.
"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# The vendor setting values and the pitcher each one uses.
_PITCHERS = {
    "anti-captcha": "AntiCaptchaProxyLess",
    "death-by-captcha": "DeathByCaptchaProxyLess",
    "captchaai": "CaptchaAIProxyLess",
}

MAX_SITE_KEY_CHARS = 512
MAX_SITE_URL_CHARS = 2048
# Every solve is paid for. At most this many run at once, across all plugins, and one that
# cannot start within the wait is refused rather than queued behind a stuck vendor.
MAX_CONCURRENT_SOLVES = 2
START_WAIT_SECONDS = 5.0
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SOLVES)


class CaptchaCallError(Exception):
    """A refusal to report to the plugin: ``code`` and a message that is safe to show."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _settings(settings=None):
    if settings is not None:
        return settings
    from app.config import settings as live

    return live


def _section(settings, name: str):
    try:
        section = settings.get(name) if hasattr(settings, "get") else getattr(settings, name, None)
    except Exception:
        section = None
    return section if section is not None else {}


def _field(section, name: str) -> str:
    value = section.get(name) if hasattr(section, "get") else getattr(section, name, None)
    return value if isinstance(value, str) else ""


def vendor_credentials(settings=None) -> tuple[str, str] | tuple[None, None]:
    """The chosen vendor's pitcher name and account key, or ``(None, None)`` without a usable choice."""
    settings = _settings(settings)
    vendor = _field(_section(settings, "general"), "anti_captcha_provider")
    if vendor == "anti-captcha":
        key = _field(_section(settings, "anticaptcha"), "anti_captcha_key")
    elif vendor == "death-by-captcha":
        section = _section(settings, "deathbycaptcha")
        username, password = _field(section, "username"), _field(section, "password")
        key = f"{username}:{password}" if username and password else ""
    elif vendor == "captchaai":
        key = _field(_section(settings, "captchaai"), "captchaai_key")
    else:
        return None, None
    return (_PITCHERS[vendor], key) if key else (None, None)


def configured_vendor(settings=None) -> str | None:
    """The vendor setting value when the user has chosen one and filled its credentials."""
    pitcher, _key = vendor_credentials(settings)
    if pitcher is None:
        return None
    return next(vendor for vendor, name in _PITCHERS.items() if name == pitcher)


def _checked_request(payload) -> tuple[str, str, bool]:
    if not isinstance(payload, dict) or payload.get("type") != "recaptcha":
        raise CaptchaCallError("unsupported", "Only reCAPTCHA tokens can be solved here.")
    site_key, site_url, invisible = payload.get("site_key"), payload.get("site_url"), payload.get("invisible", False)
    if not isinstance(site_key, str) or not site_key.strip() or len(site_key) > MAX_SITE_KEY_CHARS:
        raise CaptchaCallError("invalid", "site_key must be a short text.")
    if not isinstance(site_url, str) or len(site_url) > MAX_SITE_URL_CHARS:
        raise CaptchaCallError("invalid", "site_url must be an http or https address.")
    try:
        parts = urlsplit(site_url)
        usable = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        usable = False
    if not usable:
        raise CaptchaCallError("invalid", "site_url must be an http or https address.")
    if not isinstance(invisible, bool):
        raise CaptchaCallError("invalid", "invisible must be true or false.")
    return site_key.strip(), site_url, invisible


def _take_slot(deadline: float, cancelled: threading.Event) -> None:
    """Wait briefly for one of the solve slots, or refuse. Gives up at once when the request has ended."""
    wait_until = min(deadline, time.monotonic() + START_WAIT_SECONDS)
    while True:
        remaining = wait_until - time.monotonic()
        if remaining <= 0 or cancelled.is_set():
            raise CaptchaCallError("busy", "The captcha service is busy. Try again later.")
        if _slots.acquire(timeout=min(0.1, remaining)):
            return


def solve_call(payload, timeout: float, cancelled: threading.Event | None = None, settings=None,
               allowed=None) -> dict:
    """Answer one worker call: the reCAPTCHA response token for a site, as ``{"response": token}``.

    ``timeout`` is what is left of the request, and ``cancelled`` is set when the request ends. The solve
    gets both: past the deadline, or once cancelled, it creates no new task (so nothing more is charged),
    its waits end, and a token that arrives late is dropped. ``allowed``, when given, is asked before every
    paid step and must still say yes: a plugin whose trust was withdrawn mid-session gets nothing.

    A solve slot belongs to the solver thread and is given back only when that thread ends. A request that
    times out while the vendor is still answering therefore just returns, and the number of live vendor
    calls never goes above MAX_CONCURRENT_SOLVES however many requests time out.

    Raises CaptchaCallError, which the worker client reports to the plugin; anything else is reported as a
    plain failure.
    """
    def check_allowed():
        if allowed is not None and not allowed():
            raise CaptchaCallError("denied", "This plugin may not use the captcha service.")

    site_key, site_url, invisible = _checked_request(payload)
    check_allowed()
    pitcher_name, key = vendor_credentials(settings)
    if pitcher_name is None:
        raise CaptchaCallError("unavailable", "No captcha service is set up in Bazarr+.")
    cancelled = cancelled if cancelled is not None else threading.Event()
    deadline = time.monotonic() + max(0.0, float(timeout))
    _take_slot(deadline, cancelled)
    outcome: dict = {}

    def run():
        # The slot is this thread's: it is given back when the vendor call really ends.
        try:
            from subliminal_patch.pitcher import pitchers

            check_allowed()
            pitcher = pitchers.get_pitcher(pitcher_name)(
                "provider-hub", site_url, site_key, client_key=key, is_invisible=invisible,
                deadline=deadline, cancel=cancelled)
            outcome["token"] = pitcher.throw()
        except BaseException as error:  # reported below, never raised on this thread
            outcome["error"] = error
        finally:
            _slots.release()

    thread = threading.Thread(target=run, name="provider-captcha-solve", daemon=True)
    try:
        thread.start()
    except BaseException:
        _slots.release()
        raise
    thread.join(max(0.0, deadline - time.monotonic()))
    if thread.is_alive() or cancelled.is_set() or time.monotonic() >= deadline:
        # Stops the solve from taking another paid step; a result it still produces is not read.
        cancelled.set()
        raise CaptchaCallError("timeout", "The captcha service did not answer in time.")
    error = outcome.get("error")
    if error is not None:
        if isinstance(error, CaptchaCallError):
            raise error
        if type(error).__name__ == "CaptchaCancelled":
            raise CaptchaCallError("timeout", "The captcha service did not answer in time.")
        # The vendor's own message can name the account, so it is logged here, where the log masks
        # secrets, and never sent on.
        logger.warning("The captcha service failed a solve for %s", urlsplit(site_url).hostname, exc_info=error)
        raise CaptchaCallError("failed", "The captcha service could not solve it.")
    token = outcome.get("token")
    if not isinstance(token, str) or not token:
        raise CaptchaCallError("failed", "The captcha service returned no answer.")
    return {"response": token}
