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
            # Provider asked the host to list members (select_member) but exposes no
            # selector: reject so the host fails loud rather than risk a wrong member.
            return {"member": None, "decision": "reject"}
        provider_payload = dict(payload.get("provider_payload") or {})
        # The host forwards the requested season/episode at the top level of the op payload.
        # Surface them on provider_payload (host context is authoritative) so a selector can
        # disambiguate season-pack members even when the search payload didn't carry them.
        for key in ("season", "episode"):
            if payload.get(key) is not None:
                provider_payload[key] = payload.get(key)
        result = selector(
            provider_payload=provider_payload,
            language=payload.get("language") or {},
            members=payload.get("members") or [],
            config=payload.get("config") or {},
        ) or {}
        decision = result.get("decision")
        if decision not in ("pin", "defer", "reject"):
            decision = "reject"
        return {"member": result.get("member"), "decision": decision}
    raise ValueError(f"unsupported worker op: {op}")


def main():
    provider, _manifest = _load_provider()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = {
                "abi": ABI,
                "id": request.get("id"),
                "ok": True,
                "payload": _handle(provider, request.get("op"), request.get("payload") or {}),
                "events": [],
            }
        except Exception as error:
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            response = {
                "abi": ABI,
                "id": locals().get("request", {}).get("id") if isinstance(locals().get("request"), dict) else None,
                "ok": False,
                "error": {
                    "code": "provider",
                    "class_name": error.__class__.__name__,
                    "message": str(error),
                    "retryable": False,
                },
            }
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        if response.get("ok") and locals().get("request", {}).get("op") == "shutdown":
            break


if __name__ == "__main__":
    main()
