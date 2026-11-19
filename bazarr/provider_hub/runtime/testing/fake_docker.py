# coding=utf-8
"""A fake Docker Engine API for tests, served over a Unix socket the test makes.

It speaks just the routes ``docker_api.ALLOWED_ROUTES`` lists, keeps containers, networks and
images in memory, and records every request it gets (``requests``), so a test can say exactly which calls
a client made and which it did not. No container is ever started: ``start`` flips a flag in a dict.

    with FakeDocker() as docker:
        docker.add_registry_image("ghcr.io/o/n", DIGEST)
        client = docker.client()

Knobs a test sets on the instance:

``api_version``       the ``API-Version`` header of ``/_ping`` (None leaves it out).
``warnings``          the ``Warnings`` list ``create`` returns.
``pull_error``        the message a pull ends with, instead of success.
``health_after``      inspects of a started container that read ``starting`` before ``healthy``.
``unhealthy``         a started container turns unhealthy instead of healthy.
``exit_on_start``     an exit code: the container stops at once after start (``OOMKilled`` with ``oom``).
``deny``              regexes of ``"METHOD /path"``: matching requests get 403, as a socket proxy would answer.
``fail``              a list of ``(regex, status, message)`` consumed once each, matched like ``deny``.
``image_volumes``     anonymous volume mounts an image declares: shown in a container's ``Mounts``.
``create_returns``    a container id the ``create`` reply names instead of the new container's own.
``no_swap_accounting`` the daemon cannot limit swap: ``create`` warns and the stored ``MemorySwap`` is -1, as
                      Docker does on cgroup v1 without swap accounting.
``attach_network_id`` the network id the daemon assigns when it connects a started container (default: the id
                      of the network the container joined).
``min_api_version``   a ``"major.minor"`` string: a versioned request below it is answered 400 with the daemon's
                      "too old" text, as a daemon whose minimum is above the client's version does.
``header_delay``      seconds a pull waits, once it has arrived (see ``arrivals``), before the daemon answers at all:
                      a slow registry or a busy daemon. ``stop`` cuts the wait short.
``arrivals``          the raw path of every request as it arrived, before any delay.
``pull_delay``        seconds between the lines of a pull's progress stream, to make a pull long.
``mutate``            ``callable(inspect_dict)`` applied to every container inspect, to fake a daemon that built
                      something other than what was asked.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
import urllib.parse

from typing import Any, Callable

__all__ = ["FakeDocker", "digest_of", "forbid_real_sockets"]


def digest_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


_LIVE_SOCKETS: set[str] = set()
_LIVE_ADDRESSES: set[tuple[str, int]] = set()


def forbid_real_sockets(monkeypatch) -> None:
    """Make any Docker client connection to an existing socket that is not a fake's fail the test.

    A path that does not exist is left alone: connecting to it fails the way an absent socket does.
    """
    from .. import docker_api

    original = docker_api._UnixConnection.connect
    original_tcp = docker_api._TcpConnection.connect

    def connect(self):
        path = self._socket_path
        if path not in _LIVE_SOCKETS and os.path.exists(path):
            raise AssertionError("a test tried to open a real Docker connection")
        return original(self)

    def connect_tcp(self):
        if (self.host, self.port) not in _LIVE_ADDRESSES:
            raise AssertionError("a test tried to open a real Docker connection over tcp")
        return original_tcp(self)

    monkeypatch.setattr(docker_api._UnixConnection, "connect", connect)
    monkeypatch.setattr(docker_api._TcpConnection, "connect", connect_tcp)


class _Stream:
    """The lines of a streamed answer and the pause between them."""

    def __init__(self, lines, delay=0.0):
        self.lines = lines
        self.delay = delay


class _Quiet:
    def handle_error(self, request, client_address):
        # A client that hangs up before the answer is written (a monitor poll racing a close) is normal here.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class _Server(_Quiet, socketserver.ThreadingMixIn, http.server.HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        self.socket.bind(self.server_address)
        self.server_name = "fake-docker"
        self.server_port = 0


class _TcpServer(_Quiet, socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        return None

    def address_string(self):
        return "unix"

    def _handle(self):
        fake: FakeDocker = self.server.fake  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        fake._dispatch(self, self.command, raw)

    do_GET = do_POST = do_DELETE = _handle


class FakeDocker:
    def __init__(self, socket_path: str | None = None, *, tcp: bool = False):
        self._dir = None
        self.tcp = tcp
        self.address: tuple[str, int] | None = None
        if tcp:
            socket_path = socket_path or ""
        elif socket_path is None:
            # A short path: a Unix socket path holds at most about a hundred characters.
            self._dir = tempfile.mkdtemp(prefix="fd")
            socket_path = os.path.join(self._dir, "d.sock")
        self.socket_path = socket_path
        self.lock = threading.RLock()
        self.requests: list[dict[str, Any]] = []
        self.containers: dict[str, dict[str, Any]] = {}
        self.networks: dict[str, dict[str, Any]] = {}
        self.images: dict[str, dict[str, Any]] = {}
        self.registry: dict[str, dict[str, Any]] = {}
        self.api_version: str | None = "1.44"
        self.warnings: list[str] = []
        self.pull_error: str | None = None
        self.health_after = 1
        self.unhealthy = False
        self.exit_on_start: int | None = None
        self.oom = False
        self.deny: list[str] = []
        self.fail: list[tuple[str, int, str]] = []
        self.image_volumes: list[dict[str, Any]] = []
        self.mutate: Callable[[dict[str, Any]], None] | None = None
        self.create_returns: str | None = None
        self.attach_network_id: str | None = None
        self.min_api_version: str | None = None
        self.pull_delay = 0.0
        self.header_delay = 0.0
        self.arrivals: list[str] = []
        self._wake = threading.Event()
        self.no_swap_accounting = False
        self._counter = 0
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        if self.tcp:
            self._server = _TcpServer(("127.0.0.1", 0), _Handler)
            self.address = ("127.0.0.1", self._server.server_address[1])
            _LIVE_ADDRESSES.add(self.address)
        else:
            self._server = _Server(self.socket_path, _Handler)
            _LIVE_SOCKETS.add(self.socket_path)
        self._server.fake = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._wake.set()
        _LIVE_SOCKETS.discard(self.socket_path)
        _LIVE_ADDRESSES.discard(self.address)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)

    def client(self, **kwargs):
        from ..docker_api import DockerClient, Endpoint

        if self.tcp:
            return DockerClient(Endpoint("tcp", host=self.address[0], port=self.address[1]), **kwargs)
        return DockerClient(Endpoint("unix", path=self.socket_path), **kwargs)

    # ------------------------------------------------------------ test helpers

    def add_registry_image(self, repository: str, digest: str) -> str:
        """An image a pull by ``repository@digest`` can fetch. Returns its image id."""
        ref = f"{repository}@{digest}"
        image_id = digest_of("image:" + ref)
        self.registry[ref] = {"Id": image_id, "RepoDigests": [ref], "Architecture": "amd64", "Os": "linux"}
        return image_id

    def add_local_image(self, repository: str, digest: str) -> str:
        image_id = self.add_registry_image(repository, digest)
        ref = f"{repository}@{digest}"
        self.images[ref] = dict(self.registry[ref])
        return image_id

    def add_self(self, container_id: str | None = None, *, network_mode: str = "bridge",
                 networks: tuple[str, ...] = ("bridge",), hostname: str | None = None) -> str:
        """The container Bazarr+ itself runs in."""
        container_id = container_id or hashlib.sha256(b"self").hexdigest()
        with self.lock:
            self.containers[container_id] = {
                "Id": container_id, "Name": "/bazarr", "Image": digest_of("bazarr-image"),
                "State": {"Running": True, "Status": "running", "ExitCode": 0, "OOMKilled": False},
                "Config": {"Labels": {}, "Env": [], "Image": "bazarr:test", "Hostname": hostname or container_id[:12]},
                "HostConfig": {"NetworkMode": network_mode},
                "Mounts": [],
                "NetworkSettings": {"Networks": {name: {"IPAddress": "192.0.2.2", "Aliases": []}
                                                 for name in networks}},
                "_polls": 0, "_self": True,
            }
        return container_id

    def calls(self, method: str | None = None, pattern: str = "") -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests
                    if (method is None or r["method"] == method) and re.search(pattern, r["path"])]

    def container_named(self, name: str) -> dict[str, Any] | None:
        with self.lock:
            for record in self.containers.values():
                if record["Name"] == "/" + name:
                    return record
        return None

    # ------------------------------------------------------------ dispatch

    def _dispatch(self, handler: _Handler, method: str, raw: bytes):
        self.arrivals.append(handler.path)
        if self.header_delay and "/images/create" in handler.path:
            self._wake.wait(self.header_delay)
        parsed = urllib.parse.urlsplit(handler.path)
        path = parsed.path
        version = None
        found = re.match(r"^/v([0-9]+\.[0-9]+)(/.*)$", path)
        if found:
            version, path = found.group(1), found.group(2)
        query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        with self.lock:
            self.requests.append({"method": method, "path": path, "version": version, "query": query,
                                  "body": body, "raw_path": handler.path})
            label = f"{method} {path}"
            if version and self.min_api_version and tuple(map(int, version.split("."))) < tuple(
                    map(int, self.min_api_version.split("."))):
                return self._send(handler, 400, {"message": (
                    f"client version {version} is too old. Minimum supported API version is "
                    f"{self.min_api_version}, please upgrade your client to a newer version")})
            for pattern in self.deny:
                if re.search(pattern, label):
                    return self._send(handler, 403, {"message": "Forbidden by the socket proxy"})
            for index, (pattern, status, message) in enumerate(self.fail):
                if re.search(pattern, label):
                    del self.fail[index]
                    return self._send(handler, status, {"message": message})
            try:
                status, payload, headers = self._route(method, path, query, body)
            except KeyError as error:
                status, payload, headers = 404, {"message": f"No such object: {error.args[0]}"}, {}
        if isinstance(payload, _Stream):
            # Streamed outside the lock, so a slow stream never holds up other requests.
            return self._stream(handler, status, payload)
        return self._send(handler, status, payload, headers)

    @staticmethod
    def _send(handler, status, payload, headers=None):
        data = b"" if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        handler.send_response(status)
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    @staticmethod
    def _stream(handler, status, stream):
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        for line in stream.lines:
            handler.wfile.write(line + b"\n")
            handler.wfile.flush()
            if stream.delay:
                time.sleep(stream.delay)

    # ------------------------------------------------------------ routes

    def _route(self, method, path, query, body):
        if (method, path) == ("GET", "/_ping"):
            headers = {"Docker-Experimental": "false", "Content-Type": "text/plain"}
            if self.api_version:
                headers["API-Version"] = self.api_version
            return 200, b"OK", headers
        if method == "GET" and path == "/containers/json":
            return 200, self._list_containers(query), {}
        if (method, path) == ("POST", "/containers/create"):
            return self._create_container(query, body)
        found = re.match(r"^/containers/([^/]+)(?:/(json|start|stop))?$", path)
        if found:
            return self._container_route(method, found.group(1), found.group(2), query)
        found = re.match(r"^/images/(.+)/json$", path)
        if method == "GET" and found:
            ref = urllib.parse.unquote(found.group(1))
            if ref not in self.images:
                return 404, {"message": f"No such image: {ref}"}, {}
            return 200, self.images[ref], {}
        if (method, path) == ("POST", "/images/create"):
            return self._pull(query)
        if method == "GET" and path == "/networks":
            return 200, self._list_networks(query), {}
        if (method, path) == ("POST", "/networks/create"):
            return self._create_network(body)
        found = re.match(r"^/networks/([^/]+)(?:/(connect|disconnect))?$", path)
        if found:
            return self._network_route(method, found.group(1), found.group(2), body)
        return 404, {"message": "page not found"}, {}

    def _next(self, seed: str) -> str:
        self._counter += 1
        return hashlib.sha256(f"{seed}:{self._counter}".encode()).hexdigest()

    def _network_id(self, name: str) -> str:
        for record in self.networks.values():
            if record["Name"] == name:
                return record["Id"]
        return name

    def _find_container(self, ident: str) -> dict[str, Any]:
        if ident in self.containers:
            return self.containers[ident]
        for record in self.containers.values():
            if record["Name"] == "/" + ident or (len(ident) >= 12 and record["Id"].startswith(ident)):
                return record
        raise KeyError(ident)

    def _find_network(self, ident: str) -> dict[str, Any]:
        for record in self.networks.values():
            if ident in (record["Id"], record["Name"]):
                return record
        raise KeyError(ident)

    @staticmethod
    def _label_filter_ok(labels: dict[str, str], wanted: list[str]) -> bool:
        for item in wanted:
            key, _, value = item.partition("=")
            if labels.get(key) != value:
                return False
        return True

    def _list_containers(self, query):
        filters = json.loads(query["filters"]) if query.get("filters") else {}
        out = []
        for record in self.containers.values():
            if query.get("all") != "1" and not record["State"]["Running"]:
                continue
            if not self._label_filter_ok(record["Config"]["Labels"], filters.get("label", [])):
                continue
            nets = set(record["NetworkSettings"]["Networks"])
            ids = {n["Id"] for n in self.networks.values() if n["Name"] in nets}
            wanted = filters.get("network", [])
            if wanted and not any(item in nets or item in ids for item in wanted):
                continue
            out.append({"Id": record["Id"], "Names": [record["Name"]], "Image": record["Config"]["Image"],
                        "Labels": dict(record["Config"]["Labels"]),
                        "State": record["State"]["Status"]})
        return out

    def _create_container(self, query, body):
        name = query.get("name") or self._next("anon")[:12]
        if any(r["Name"] == "/" + name for r in self.containers.values()):
            return 409, {"message": f"Conflict. The container name \"/{name}\" is already in use"}, {}
        ref = body["Image"]
        if ref not in self.images:
            return 404, {"message": f"No such image: {ref}"}, {}
        host = dict(body["HostConfig"])
        for key in ("Binds", "Devices", "CapAdd", "VolumesFrom", "Links", "ExtraHosts", "GroupAdd"):
            host.setdefault(key, None)
        host.setdefault("PidMode", "")
        host.setdefault("UsernsMode", "")
        host.setdefault("UTSMode", "")
        host.setdefault("Privileged", False)
        network = host["NetworkMode"]
        if network not in [n["Name"] for n in self.networks.values()] + ["bridge", "host", "none"]:
            return 404, {"message": f"network {network} not found"}, {}
        cid = self._next(name)
        mounts = [{"Type": "volume", "Destination": v["Destination"], "Name": self._next("vol")[:16]}
                  for v in self.image_volumes]
        mounts += [{"Type": "tmpfs", "Destination": p} for p in (host.get("Tmpfs") or {})]
        state = {"Running": False, "Status": "created", "ExitCode": 0, "OOMKilled": False}
        if body.get("Healthcheck"):
            state["Health"] = {"Status": "starting"}
        endpoints = (body.get("NetworkingConfig") or {}).get("EndpointsConfig") or {network: {}}
        self.containers[cid] = {
            "Id": cid, "Name": "/" + name, "Image": self.images[ref]["Id"], "State": state,
            "Config": {"Labels": dict(body.get("Labels") or {}), "Env": list(body.get("Env") or []),
                       "Image": ref, "Hostname": cid[:12], "Healthcheck": body.get("Healthcheck")},
            "HostConfig": host, "Mounts": mounts,
            # As sent: the daemon fills the id and the address in when the start connects the network.
            "NetworkSettings": {"Networks": {n: {"IPAddress": "", "NetworkID": (e or {}).get("NetworkID", ""),
                                                 "Aliases": list((e or {}).get("Aliases") or [])}
                                             for n, e in endpoints.items()}},
            "_polls": 0,
        }
        warnings = list(self.warnings)
        if self.no_swap_accounting:
            warnings.append("Your kernel does not support swap limit capabilities or the cgroup is not mounted. "
                            "Memory limited without swap.")
            host["MemorySwap"] = -1
        return 201, {"Id": self.create_returns or cid, "Warnings": warnings}, {}

    def _public(self, record):
        view = json.loads(json.dumps({k: v for k, v in record.items() if not k.startswith("_")}))
        if self.mutate is not None:
            self.mutate(view)
        return view

    def _container_route(self, method, ident, action, query):
        record = self._find_container(ident)
        state = record["State"]
        if method == "GET" and action == "json":
            if state["Running"] and "Health" in state:
                record["_polls"] += 1
                if self.unhealthy:
                    state["Health"]["Status"] = "unhealthy"
                elif record["_polls"] > self.health_after:
                    state["Health"]["Status"] = "healthy"
            return 200, self._public(record), {}
        if method == "POST" and action == "start":
            if state["Running"]:
                return 304, None, {}
            if self.exit_on_start is not None:
                state.update(Running=False, Status="exited", ExitCode=self.exit_on_start, OOMKilled=self.oom)
            else:
                state.update(Running=True, Status="running", ExitCode=0)
            for index, (name, endpoint) in enumerate(record["NetworkSettings"]["Networks"].items()):
                endpoint["NetworkID"] = self.attach_network_id or self._network_id(name)
                endpoint["IPAddress"] = "198.51.100.%d" % (2 + index + len(self.containers))
            return 204, None, {}
        if method == "POST" and action == "stop":
            if not state["Running"]:
                return 304, None, {}
            state.update(Running=False, Status="exited", ExitCode=137)
            return 204, None, {}
        if method == "DELETE" and action is None:
            if state["Running"] and query.get("force") != "1":
                return 409, {"message": "You cannot remove a running container"}, {}
            del self.containers[record["Id"]]
            return 204, None, {}
        return 404, {"message": "page not found"}, {}

    def _list_networks(self, query):
        filters = json.loads(query["filters"]) if query.get("filters") else {}
        out = []
        for record in self.networks.values():
            if filters.get("name") and not any(w in record["Name"] for w in filters["name"]):
                continue
            if not self._label_filter_ok(record["Labels"], filters.get("label", [])):
                continue
            out.append(dict(record))
        return out

    def _create_network(self, body):
        if any(n["Name"] == body["Name"] for n in self.networks.values()):
            return 409, {"message": f"network with name {body['Name']} already exists"}, {}
        nid = self._next(body["Name"])
        self.networks[nid] = {"Id": nid, "Name": body["Name"], "Driver": body.get("Driver"),
                              "Internal": body.get("Internal", False), "Labels": dict(body.get("Labels") or {}),
                              "Containers": {}}
        return 201, {"Id": nid, "Warning": ""}, {}

    def _network_route(self, method, ident, action, body):
        record = self._find_network(ident)
        attached = [c for c in self.containers.values() if record["Name"] in c["NetworkSettings"]["Networks"]]
        if method == "GET" and action is None:
            view = dict(record)
            view["Containers"] = {c["Id"]: {"Name": c["Name"].lstrip("/")} for c in attached}
            return 200, view, {}
        if method == "DELETE" and action is None:
            if attached:
                return 403, {"message": f"error while removing network: network {record['Name']} has active endpoints"}, {}
            del self.networks[record["Id"]]
            return 204, None, {}
        if method == "POST" and action == "connect":
            container = self._find_container(body["Container"])
            aliases = (body.get("EndpointConfig") or {}).get("Aliases") or []
            container["NetworkSettings"]["Networks"][record["Name"]] = {
                "IPAddress": "198.51.100.200", "NetworkID": record["Id"], "Aliases": aliases}
            return 200, None, {}
        if method == "POST" and action == "disconnect":
            container = self._find_container(body["Container"])
            container["NetworkSettings"]["Networks"].pop(record["Name"], None)
            return 200, None, {}
        return 404, {"message": "page not found"}, {}

    def _pull(self, query):
        ref = query.get("fromImage", "")
        if self.pull_error:
            return 200, _Stream([json.dumps({"status": "Pulling from x"}).encode(),
                                 json.dumps({"errorDetail": {"message": self.pull_error},
                                             "error": self.pull_error}).encode()]), {}
        if ref not in self.registry:
            return 404, {"message": f"manifest for {ref} not found: manifest unknown"}, {}
        lines = [json.dumps({"status": "Pulling from x"}).encode()]
        if self.pull_delay:
            lines += [json.dumps({"status": "Downloading", "id": str(index)}).encode() for index in range(600)]
        lines += [json.dumps({"status": "Digest: " + ref.split("@", 1)[1]}).encode(),
                  json.dumps({"status": "Status: Downloaded newer image"}).encode()]
        self.images[ref] = dict(self.registry[ref])
        return 200, _Stream(lines, self.pull_delay), {}
