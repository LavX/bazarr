# coding=utf-8
"""Observe a fresh native file scan before acknowledging a subtitle mutation."""

import asyncio
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket

import aiohttp
import dns.asyncresolver
import dns.exception

from media_servers.http import MediaServerError, MediaServerHTTP
from media_servers.paths import map_media_path, media_paths_equal


_ACTIVE = {"accepted", "running"}
_TERMINAL = {"completed", "failed", "cancelled"}
_EVENT_STATUS = {"scan.accepted": "accepted", "scan.started": "running", "scan.progress": "running",
                 "scan.completed": "completed", "scan.failed": "failed", "scan.cancelled": "cancelled"}
_CLEAN_COUNTERS = ("skipped", "errors", "missing", "missing_skipped_protected")
_SNAPSHOT_LIMIT = 500
_OBSERVED_LIMIT = 2048
_POST_LIMIT = 3
_NATIVE_INT_MAX = 2**63 - 1
_RFC3339 = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})")


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 35:
        return None
    match = _RFC3339.fullmatch(value)
    if match is None:
        return None
    try:
        stamp = datetime.fromisoformat(match[1] + match[3])
        elapsed = stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None
    seconds = elapsed.days * 86400 + elapsed.seconds
    return seconds * 1_000_000_000 + int((match[2] or "0").ljust(9, "0"))


class _NativeResolver(aiohttp.abc.AbstractResolver):
    """Cancellable DNS plus hosts-file names without a blocking resolver thread."""

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        addresses = []
        hosts_path = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/drivers/etc/hosts"
                      if os.name == "nt" else Path("/etc/hosts"))
        try:
            with hosts_path.open(encoding="utf-8", errors="replace") as hosts:
                for line in hosts:
                    fields = line.partition("#")[0].split()
                    if len(fields) < 2 or host.rstrip(".").lower() not in [name.lower() for name in fields[1:]]:
                        continue
                    try:
                        address = ipaddress.ip_address(fields[0])
                    except ValueError:
                        continue
                    address_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
                    if family in {socket.AF_UNSPEC, address_family}:
                        addresses.append((str(address), address_family))
        except OSError:
            pass
        if not addresses:
            try:
                # Preserve configured nameservers, search domains and ndots,
                # including Docker's embedded resolver. Every DNS socket belongs
                # to a cancellable async query within the connector's deadline.
                resolver = dns.asyncresolver.Resolver()
                answer = await resolver.resolve_name(host, family=family, search=True, lifetime=3)
                addresses = list(answer.addresses_and_families())
            except dns.exception.Timeout:
                raise TimeoutError from None
            except dns.exception.DNSException:
                raise OSError("connection_error") from None
        if not addresses:
            raise OSError("connection_error")
        return [{"hostname": host, "host": address, "port": port, "family": address_family,
                 "proto": 0, "flags": socket.AI_NUMERICHOST} for address, address_family in addresses]

    async def close(self):
        # dnspython's async query contexts own and close their sockets.
        pass


def _status_error(status):
    if 300 <= status < 400:
        return "redirect_denied"
    return {401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(
        status, "server_error" if status >= 500 else "request_rejected")


async def _reject_redirect(_session, _context, params):
    # ws_connect has no allow_redirects argument. This hook runs before aiohttp
    # follows Location, including same-origin redirects that retain the key.
    params.response.close()
    raise MediaServerError("redirect_denied")


def _decode_json(body):
    try:
        return json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise MediaServerError("invalid_response") from None


async def _receive(ws):
    message = await ws.receive()
    if message.type == aiohttp.WSMsgType.TEXT:
        payload = _decode_json(message.data)
        if not isinstance(payload, dict):
            raise MediaServerError("invalid_response")
        return payload
    if (message.type == aiohttp.WSMsgType.ERROR and isinstance(message.data, aiohttp.WebSocketError)
            and message.data.code == aiohttp.WSCloseCode.MESSAGE_TOO_BIG):
        raise MediaServerError("response_too_large")
    if message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR}:
        raise MediaServerError("stream_disconnected")
    raise MediaServerError("invalid_response")


def _validate_run(row):
    if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
            or type(row.get("library_id")) is not int or not 0 < row["library_id"] <= _NATIVE_INT_MAX
            or not isinstance(row.get("mode"), str) or not isinstance(row.get("status"), str)
            or row.get("mode") not in {"file", "subtree", "library"}
            or row.get("status") not in _ACTIVE | _TERMINAL
            or not isinstance(row.get("trigger"), str)):
        raise MediaServerError("invalid_response")
    if row["mode"] == "library":
        if "path" in row and (not isinstance(row["path"], str)
                              or row["path"] and not media_paths_equal(row["path"], row["path"])):
            raise MediaServerError("invalid_response")
    elif not media_paths_equal(row.get("path"), row.get("path")):
        raise MediaServerError("invalid_response")
    result = row.get("result")
    if result is not None and (not isinstance(result, dict) or any(
            type(result.get(key)) is not int or not 0 <= result[key] <= _NATIVE_INT_MAX for key in _CLEAN_COUNTERS)):
        raise MediaServerError("invalid_response")


def _same_target(left, right):
    return (left["library_id"] == right["library_id"] and left["mode"] == right["mode"]
            and (left["mode"] == "library" or media_paths_equal(left.get("path"), right.get("path"))))


class _Observation:
    def __init__(self, library_id, video_path):
        self.library_id = library_id
        self.video_path = video_path
        self.runs = {}
        self.sequence = 0
        self.watermark = None
        self.retained_text = 0
        self.changed = asyncio.Event()
        self.failure = None

    def overlaps(self, row):
        if row["library_id"] != self.library_id:
            return False
        if row["mode"] == "library":
            return True
        if row["mode"] == "file":
            return media_paths_equal(row["path"], self.video_path)
        try:
            map_media_path(self.video_path, [{"local_path": row["path"], "remote_path": row["path"]}])
            return True
        except MediaServerError as error:
            if error.code == "mapping_missing":
                return False
            raise

    def idle(self):
        return not any(run["row"]["status"] in _ACTIVE and self.overlaps(run["row"])
                       for run in self.runs.values())

    def check(self):
        if self.failure:
            raise MediaServerError(self.failure)

    async def wait(self):
        self.check()
        self.changed.clear()
        await self.changed.wait()
        self.check()

    async def wait_idle(self):
        self.check()
        while not self.idle():
            await self.wait()

    def remember(self, row, *, accepted=False, event_time=None):
        _validate_run(row)
        # Retain only the target, status and clean-result evidence. Progress text,
        # arbitrary result extensions and server error messages are unnecessary.
        result = row.get("result")
        started, completed = _timestamp(row.get("started_at")), _timestamp(row.get("completed_at"))
        row = {key: row.get(key) for key in ("id", "library_id", "mode", "path", "status")}
        if row["mode"] == "library":
            row["path"] = None
        row["result"] = ({key: result.get(key) for key in _CLEAN_COUNTERS} if isinstance(result, dict) else None)
        row.update(started_at=started, completed_at=completed)
        prior = self.runs.get(row["id"])
        charge = 4 * (len(row["id"]) + len(row.get("path") or ""))
        previous_charge = 4 * (len(prior["row"]["id"]) + len(prior["row"].get("path") or "")) if prior else 0
        retained_text = self.retained_text - previous_charge + charge
        if retained_text > MediaServerHTTP.RESPONSE_LIMIT or prior is None and len(self.runs) >= _OBSERVED_LIMIT:
            raise MediaServerError("observation_incomplete")
        self.sequence += 1
        if prior:
            if not _same_target(prior["row"], row):
                raise MediaServerError("invalid_response")
            if prior["row"]["status"] in _TERMINAL and row["status"] != prior["row"]["status"]:
                raise MediaServerError("observation_incomplete")
            prior["ambiguous"] |= (event_time is None or prior["last_time"] is not None
                                    and event_time < prior["last_time"]
                                    or prior["row"]["started_at"] is not None
                                    and prior["row"]["started_at"] != started)
            # Keep the original ID object shared with the map key on replacement.
            row["id"] = prior["row"]["id"]
            prior["row"] = row
            prior["last_time"] = event_time
        else:
            self.runs[row["id"]] = {"row": row, "first": self.sequence,
                                    "accepted": self.sequence if accepted else 0, "accepted_time": event_time,
                                    "last_time": event_time, "ambiguous": event_time is None}
        self.retained_text = retained_text
        for stamp in (event_time, started, completed):
            if stamp is not None:
                self.watermark = stamp if self.watermark is None else max(self.watermark, stamp)
        self.changed.set()

    def event(self, payload):
        if payload.get("type") != "event" or payload.get("channel") != "scans":
            raise MediaServerError("observation_incomplete")
        event = payload.get("event")
        if (not isinstance(event, str) or event not in _EVENT_STATUS
                or not isinstance(payload.get("event_id"), str) or not payload["event_id"]):
            raise MediaServerError("invalid_response")
        row = payload.get("data")
        _validate_run(row)
        if row["status"] != _EVENT_STATUS[event]:
            raise MediaServerError("invalid_response")
        self.remember(row, accepted=event == "scan.accepted", event_time=_timestamp(payload.get("timestamp")))


async def _handshake(ws, state):
    hello = await _receive(ws)
    if (hello.get("type") != "hello" or type(hello.get("schema_version")) is not int
            or hello["schema_version"] != 1 or hello.get("required_action") != "none"
            or not isinstance(hello.get("connection_id"), str) or not hello["connection_id"]
            or not isinstance(hello.get("available_channels"), list) or "scans" not in hello["available_channels"]):
        raise MediaServerError("observation_incomplete")
    subscribed = await _receive(ws)
    if (subscribed.get("type") != "subscribed" or not isinstance(subscribed.get("channels"), list)
            or "scans" not in subscribed["channels"]):
        raise MediaServerError("observation_incomplete")
    snapshot = await _receive(ws)
    if (snapshot.get("type") != "snapshot" or snapshot.get("channel") != "scans"
            or not isinstance(snapshot.get("data"), list) or len(snapshot["data"]) >= _SNAPSHOT_LIMIT):
        raise MediaServerError("observation_incomplete")
    # Native snapshots are timestamped after the active-run query. Envelopes
    # queued during that query retain their older publication timestamps.
    state.watermark = _timestamp(snapshot.get("timestamp"))
    if state.watermark is None:
        raise MediaServerError("observation_incomplete")
    for row in snapshot["data"]:
        _validate_run(row)
        if row["status"] not in _ACTIVE or row["id"] in state.runs:
            raise MediaServerError("observation_incomplete")
        state.remember(row)


async def _listen(ws, state):
    try:
        while True:
            state.event(await _receive(ws))
            # A busy buffered stream must still allow the deadline and HTTP task to run.
            await asyncio.sleep(0)
    except MediaServerError as error:
        state.failure = error.code
    except (aiohttp.ClientError, OSError):
        state.failure = "stream_disconnected"
    except (TypeError, ValueError):
        state.failure = "invalid_response"
    finally:
        state.changed.set()


async def _post_scan(session, url, library_id, video_path, verify_ssl):
    async with session.post(url + "/api/v1/scan", json={"library_id": library_id, "path": video_path},
                            ssl=verify_ssl, allow_redirects=False) as response:
        if response.status != 202:
            raise MediaServerError(_status_error(response.status))
        if response.content_length is not None and response.content_length > MediaServerHTTP.RESPONSE_LIMIT:
            raise MediaServerError("response_too_large")
        body = bytearray()
        async for chunk in response.content.iter_chunked(8192):
            if len(body) + len(chunk) > MediaServerHTTP.RESPONSE_LIMIT:
                raise MediaServerError("response_too_large")
            body.extend(chunk)
        result = _decode_json(body)
        if (not isinstance(result, dict) or result.get("status") != "accepted" or result.get("mode") != "file"
                or type(result.get("library_id")) is not int or result["library_id"] != library_id):
            raise MediaServerError("invalid_response")


def _clean_completion(row):
    if row["status"] in {"failed", "cancelled"}:
        raise MediaServerError("scan_" + row["status"])
    if row["status"] != "completed":
        return False
    result = row.get("result")
    if (not isinstance(result, dict)
            or any(type(result.get(key)) is not int or result[key] < 0 for key in _CLEAN_COUNTERS)):
        raise MediaServerError("invalid_response")
    if any(result[key] for key in _CLEAN_COUNTERS):
        raise MediaServerError("scan_incomplete")
    return True


def _causal_completion(run, boundary):
    row = run["row"]
    accepted, started = run["accepted_time"], row["started_at"]
    completed, published = row["completed_at"], run["last_time"]
    return (not run["ambiguous"] and all(stamp is not None for stamp in (accepted, started, completed, published))
            and accepted > boundary and boundary < started <= completed <= published and accepted <= published)


async def _submit_and_observe(session, http, state, ensure_current=None):
    for _attempt in range(_POST_LIMIT):
        await state.wait_idle()
        if ensure_current:
            ensure_current()
        baseline = state.sequence
        boundary = state.watermark
        if boundary is None:
            raise MediaServerError("observation_incomplete")
        await _post_scan(session, http.url, state.library_id, state.video_path, http.verify_ssl)
        while True:
            state.check()
            fresh = [run for run in state.runs.values() if run["first"] > baseline and state.overlaps(run["row"])]
            candidates = [run for run in fresh if run["accepted"] > baseline and run["accepted_time"] is not None
                          and run["accepted_time"] > boundary and run["row"]["mode"] == "file"
                          and media_paths_equal(run["row"]["path"], state.video_path)]
            if len(candidates) > 1:
                raise MediaServerError("observation_incomplete")
            if candidates and _clean_completion(candidates[0]["row"]):
                if len(fresh) == 1 and _causal_completion(candidates[0], boundary):
                    if ensure_current:
                        ensure_current()
                    return {"status": "confirmed"}
                # Concurrent parent/library work can skip this file or publish a stale
                # directory cache later. Missing/old run times are also ambiguous.
                # Wait it out, then require another run beyond the updated boundary.
                break
            if not candidates and fresh and state.idle():
                # A reused run or lost accepted event is not causal proof. Its terminal
                # event only removes a barrier, so submit a bounded trailing scan.
                break
            await state.wait()
    raise MediaServerError("observation_incomplete")


async def _refresh(http, library_id, video_path, timeout, ensure_current=None):
    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(_reject_redirect)
    network_timeout = aiohttp.ClientTimeout(total=None, connect=3, sock_connect=3, sock_read=10)
    connector = aiohttp.TCPConnector(resolver=_NativeResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(headers={"Authorization": http.session.headers["Authorization"],
                                             "Accept": "application/json"}, trust_env=False,
                                     cookie_jar=aiohttp.DummyCookieJar(), timeout=network_timeout,
                                     trace_configs=[trace], connector=connector) as session:
        # One event-loop monotonic deadline includes handshake, barriers, requests,
        # streamed bodies, terminal events and graceful close. Session exit aborts
        # transports on error or expiry instead of waiting for a close handshake.
        async with asyncio.timeout(timeout):
            if ensure_current:
                ensure_current()
            ws = await session.ws_connect(http.url + "/api/v1/events/ws", params={"channels": "scans"},
                                          ssl=http.verify_ssl, max_msg_size=MediaServerHTTP.RESPONSE_LIMIT,
                                          timeout=aiohttp.ClientWSTimeout(ws_close=0))
            state = _Observation(library_id, video_path)
            await _handshake(ws, state)
            listener = asyncio.create_task(_listen(ws, state))
            try:
                result = await _submit_and_observe(session, http, state, ensure_current)
            finally:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
            await ws.close()
            if ensure_current:
                ensure_current()
            return result


def native_library_id(library_id):
    """The saved library id as the native integer Silo answers to."""
    if (not isinstance(library_id, str) or not library_id or not library_id.isascii() or not library_id.isdecimal()
            or len(library_id.lstrip("0")) > 19):
        raise MediaServerError("library_invalid")
    native_id = int(library_id.lstrip("0") or "0")
    if not 0 < native_id <= 2**63 - 1:
        raise MediaServerError("library_invalid")
    return native_id


def refresh_library(http, library_id, *, ensure_current=None):
    """Ask Silo to rescan a whole library, when it cannot scan the file itself.

    Silo's file scan reads a closed set of containers and refuses any path it
    cannot place in the library, and retrying that request cannot change the
    answer. The library scan is the broad request Jellyfin falls back to.

    It is submitted rather than observed, unlike the file scan: a full scan
    outlives any deadline worth holding a worker on, and Silo deduplicates a
    second one silently while still answering 202, so a completion event is
    not evidence that this request caused anything.
    """
    native_id = native_library_id(library_id)
    if ensure_current:
        ensure_current()
    result = http.request_json("POST", "/api/v1/scan", json={"library_id": native_id},
                               success_statuses=(202,))
    if (not isinstance(result, dict) or result.get("status") != "accepted" or result.get("mode") != "library"
            or type(result.get("library_id")) is not int or result["library_id"] != native_id):
        raise MediaServerError("invalid_response")
    if ensure_current:
        ensure_current()
    return {"status": "requested"}


def refresh_file(http, library_id, video_path, *, timeout=90.0, ensure_current=None):
    native_id = native_library_id(library_id)
    if not media_paths_equal(video_path, video_path):
        raise MediaServerError("path_invalid")
    if type(timeout) not in {int, float} or timeout <= 0 or (type(timeout) is float and not math.isfinite(timeout)):
        raise MediaServerError("invalid_timeout")
    if timeout > 2**63 - 1:
        raise MediaServerError("invalid_timeout")
    try:
        return asyncio.run(_refresh(http, native_id, video_path, min(timeout, 90.0), ensure_current))
    except MediaServerError:
        raise
    except TimeoutError:
        raise MediaServerError("timeout") from None
    except (aiohttp.ClientSSLError, aiohttp.ClientConnectorCertificateError):
        raise MediaServerError("tls_error") from None
    except aiohttp.WSServerHandshakeError as error:
        raise MediaServerError(_status_error(error.status)) from None
    except (aiohttp.ClientError, OSError, ValueError):
        raise MediaServerError("connection_error") from None
