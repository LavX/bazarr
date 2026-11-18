#!/usr/bin/env python3
# coding=utf-8
"""Standalone Provider Hub worker runner.

This file intentionally avoids importing Bazarr modules so it can run under a
provider venv with isolated site-packages and `python -I`.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import sys
import threading
import traceback
import types
import uuid

ABI = "bazarr.provider-worker.v1"

# The reply is a single JSON line, so a traceback longer than this is cut to
# its tail, where the raising frame is: that is the part the host logs.
_ERROR_TRACEBACK_LIMIT = 8000


def _error_payload(error):
    """The error half of a failed worker's reply.

    The plugin's own traceback travels with it, cut to its last 8,000
    characters: the host's traceback for a plugin failure ends in the
    transport for every plugin and every cause, so without this the log
    cannot point at the plugin's frame. The errno is set only for an OSError
    the operating system raised: the plugins raise PermissionError with a
    message of their own for a refused login, and that form never carries
    one, so its presence tells the two apart.
    """
    payload = {
        "code": "provider",
        "class_name": error.__class__.__name__,
        "message": str(error),
        # A rate limit the provider timed for us. The host rebuilds
        # the exception from the class name and message alone, so
        # without carrying this the provider's own "come back in an
        # hour" is lost at the boundary and its backoff falls back
        # to whatever the exception class is worth in general.
        "retry_after": _retry_after(error),
        "retryable": False,
        "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__))[
            -_ERROR_TRACEBACK_LIMIT:],
    }
    if isinstance(error, OSError) and isinstance(error.errno, int) and not isinstance(error.errno, bool):
        payload["errno"] = error.errno
    return payload


class HostCallError(RuntimeError):
    """The host refused or failed a call a plugin made through ``bazarr_host``."""

    def __init__(self, message, code="failed"):
        super().__init__(message)
        self.code = code


# The id of the request being served and a lock for the one stdin and stdout pair,
# so threads of a plugin never interleave their frames.
_current = {"request_id": None}
_call_lock = threading.Lock()


def _granted():
    try:
        value = json.loads(os.environ.get("BAZARR_HOST_CALLS") or "[]")
    except ValueError:
        return ()
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def _host_call(capability, payload=None):
    """Ask the host to do something on this plugin's behalf and return its answer.

    The call travels over the pipes this worker already has: a frame on stdout while a
    request is being served, the reply on stdin. There is no socket and no address, so
    nothing here needs the network. The host decides whether this plugin may use the
    capability, and a refusal arrives as a HostCallError.
    """
    if _current["request_id"] is None:
        raise HostCallError("a host call is only possible while a request is being served", "unavailable")
    call_id = uuid.uuid4().hex
    frame = {
        "abi": ABI,
        "id": _current["request_id"],
        "call": {"id": call_id, "capability": str(capability), "payload": payload or {}},
    }
    with _call_lock:
        sys.stdout.write(json.dumps(frame, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        line = sys.stdin.readline()
    if not line:
        raise HostCallError("the host closed the channel", "unavailable")
    try:
        reply = json.loads(line)
    except ValueError:
        raise HostCallError("the host sent a malformed reply", "failed") from None
    answer = reply.get("call_reply") if isinstance(reply, dict) else None
    if (not isinstance(answer, dict) or reply.get("abi") != ABI or reply.get("id") != _current["request_id"]
            or answer.get("id") != call_id):
        raise HostCallError("the host sent an unexpected reply", "failed")
    if answer.get("ok") is True and isinstance(answer.get("payload"), dict):
        return answer["payload"]
    error = answer.get("error") if isinstance(answer.get("error"), dict) else {}
    raise HostCallError(str(error.get("message") or "the host call failed"), str(error.get("code") or "failed"))


def _install_host_module():
    """The ``bazarr_host`` module a plugin imports to reach what the host provides."""
    module = types.ModuleType("bazarr_host")
    module.HostCallError = HostCallError
    module.granted = _granted
    module.call = _host_call

    def solve_recaptcha(site_key, site_url, invisible=False):
        """A reCAPTCHA response token from the captcha service the user set up in Bazarr+."""
        answer = _host_call("host.captcha.v1", {
            "type": "recaptcha", "site_key": site_key, "site_url": site_url, "invisible": bool(invisible)})
        return answer.get("response")

    module.solve_recaptcha = solve_recaptcha
    sys.modules["bazarr_host"] = module


def _load_provider():
    _install_host_module()
    bundle_path = os.environ["BAZARR_PROVIDER_HUB_BUNDLE"]
    manifest = json.loads(os.environ["BAZARR_PROVIDER_HUB_MANIFEST"])
    sys.path.insert(0, bundle_path)
    module = importlib.import_module(manifest["entry_module"])
    cls = getattr(module, manifest["entry_class"])
    return cls(), manifest


def _content_payload(result):
    if result is None:
        return {"empty": True}
    if isinstance(result, bytes):
        content = result
        return {
            "content_b64": base64.b64encode(content).decode("ascii"),
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "empty": False,
        }
    if isinstance(result, str):
        content = result.encode("utf-8")
        return {
            "content_b64": base64.b64encode(content).decode("ascii"),
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "encoding": "utf-8",
            "empty": False,
        }
    if isinstance(result, dict) and "content" in result and "content_b64" not in result:
        content = result.pop("content")
        if isinstance(content, str):
            content = content.encode("utf-8")
        result["content_b64"] = base64.b64encode(content).decode("ascii")
        result["content_sha256"] = hashlib.sha256(content).hexdigest()
        result.setdefault("empty", False)
    return result



def _retry_after(error):
    """The provider's own Retry-After, if it gave one, as a plain number.

    Only a finite, positive value crosses: a plugin is untrusted code, and this
    number decides how long the host stops asking for. Bounded at a day, the
    same ceiling the host applies to the value it reads back.
    """
    value = getattr(error, "retry_after", None)
    try:
        value = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if value is None or value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return None
    return min(86400.0, value)


def _drain_events(provider):
    try:
        drain = getattr(provider, "drain_events", None)
        if drain is None:
            return []
        reported = drain()
        if not isinstance(reported, list):
            return []
        events = []
        encoded_size = 2
        for event in reported:
            if len(events) == 8:
                break
            if not isinstance(event, dict):
                continue
            try:
                encoded = json.dumps(event, separators=(",", ":"), allow_nan=False)
                decoded = json.loads(encoded)
            except Exception:
                print(traceback.format_exc(), file=sys.stderr, flush=True)
                continue
            event_size = len(encoded.encode("utf-8")) + (1 if events else 0)
            if encoded_size + event_size > 4096:
                continue
            events.append(decoded)
            encoded_size += event_size
        return events
    except Exception:
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        return []


def _handle(provider, op, payload):
    if op == "health":
        return {"initialized": True}
    if op == "shutdown":
        return {"accepted": True}
    if op == "search":
        candidates = provider.search(
            video=payload.get("video") or {},
            languages=payload.get("languages") or [],
            config=payload.get("config") or {},
        )
        return {"candidates": candidates or []}
    if op == "download":
        result = provider.download(
            provider_payload=payload.get("provider_payload") or {},
            language=payload.get("language") or {},
            config=payload.get("config") or {},
        )
        return _content_payload(result)
    if op == "select_archive_member":
        selector = getattr(provider, "select_archive_member", None)
        if selector is None:
            raise ValueError("select_archive_member is not implemented")
        provider_payload = dict(payload.get("provider_payload") or {})
        # The host forwards relative/absolute episode context at the top level of the op payload.
        # Surface them on provider_payload (host context is authoritative) so a selector can
        # disambiguate season-pack members even when the search payload didn't carry them.
        # An explicit null clears stale context; an absent key preserves legacy payloads.
        for key in ("season", "episode", "absolute_episode"):
            if key in payload:
                provider_payload[key] = payload[key]
        result = selector(
            provider_payload=provider_payload,
            language=payload.get("language") or {},
            members=payload.get("members") or [],
            config=payload.get("config") or {},
        )
        if not isinstance(result, dict):
            raise ValueError("select_archive_member must return an object")
        decision = result.get("decision")
        if decision not in ("pin", "defer", "reject"):
            raise ValueError("select_archive_member returned an invalid decision")
        return {"member": result.get("member"), "decision": decision}
    raise ValueError(f"unsupported worker op: {op}")


def main():
    provider, _manifest = _load_provider()
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            _current["request_id"] = request.get("id")
            response = {
                "abi": ABI,
                "id": request.get("id"),
                "ok": True,
                "payload": _handle(provider, request.get("op"), request.get("payload") or {}),
                "events": _drain_events(provider) if request.get("op") in ("search", "download") else [],
            }
        except Exception as error:
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            if isinstance(request, dict) and request.get("op") in ("search", "download"):
                _drain_events(provider)
            response = {
                "abi": ABI,
                "id": request.get("id") if isinstance(request, dict) else None,
                "ok": False,
                "error": _error_payload(error),
            }
        _current["request_id"] = None
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        if response.get("ok") and isinstance(request, dict) and request.get("op") == "shutdown":
            break


if __name__ == "__main__":
    main()
