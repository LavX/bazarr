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
import traceback

ABI = "bazarr.provider-worker.v1"


def _load_provider():
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
                "error": {
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
                },
            }
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        if response.get("ok") and isinstance(request, dict) and request.get("op") == "shutdown":
            break


if __name__ == "__main__":
    main()
