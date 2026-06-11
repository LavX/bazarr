# coding=utf-8
"""Request-handling logic for the arr_instances CRUD API (#156).

Each function takes an explicit SQLAlchemy session plus parsed request args and
returns a ``(body, status_code)`` tuple. Keeping the logic here (rather than in
the Flask resources) makes it unit-testable without the heavy ``api`` import
chain, and keeps the resources to thin parse/commit glue.

The session is flushed but NOT committed here; the HTTP boundary owns the
transaction and commits on success.
"""
from sqlalchemy.exc import IntegrityError

from .repository import VALID_KINDS, ArrInstanceRepository, to_safe_dict

_CONFLICT_MESSAGE = "An instance with these connection properties already exists."


def _connection_arg_error(args):
    """Return a validation message for out-of-range connection args, else None.

    Mirrors the scalar config validators (port 1-65535, positive timeout) so the
    API rejects bad values instead of storing them or silently coercing them.
    """
    port = args.get("port")
    if port is not None and not (1 <= port <= 65535):
        return "port must be between 1 and 65535"
    timeout = args.get("http_timeout")
    if timeout is not None and timeout <= 0:
        return "http_timeout must be a positive number of seconds"
    return None


def test_connection(args, http_get=None):
    """Probe a Sonarr/Radarr instance described by raw body params.

    Reads connection details (including the plaintext API key) only from the
    request body, never from the URL/query. Returns ``(result, 200)`` where
    result carries ok/version or a structured error; an invalid kind is 400.
    """
    from .client import ArrClientFactory

    kind = args.get("kind")
    if kind not in VALID_KINDS:
        return {"ok": False, "error": "invalid", "message": "invalid kind"}, 400
    arg_error = _connection_arg_error(args)
    if arg_error:
        return {"ok": False, "error": "invalid", "message": arg_error}, 400

    client = ArrClientFactory().from_params(
        kind=kind,
        ip=args.get("ip") or "127.0.0.1",
        port=args.get("port"),
        base_url=args.get("base_url") or "/",
        ssl=bool(args.get("ssl")),
        verify_ssl=bool(args.get("verify_ssl")),
        api_key=args.get("api_key") or "",
        http_timeout=args.get("http_timeout") or 60,
        http_get=http_get,
    )
    return client.test_connection(), 200


def list_instances(session, kind=None):
    repo = ArrInstanceRepository(session)
    return [to_safe_dict(i) for i in repo.list(kind=kind)], 200


def get_instance(session, instance_id):
    repo = ArrInstanceRepository(session)
    row = repo.get(instance_id)
    if row is None:
        return {"error": "not_found"}, 404
    return to_safe_dict(row), 200


def create_instance(session, args):
    arg_error = _connection_arg_error(args)
    if arg_error:
        return {"error": "invalid", "message": arg_error}, 400
    repo = ArrInstanceRepository(session)
    try:
        row = repo.create(
            args.get("kind"),
            args.get("name"),
            api_key=args.get("api_key") or "",
            ip=args.get("ip") or "127.0.0.1",
            port=args.get("port"),
            base_url=args.get("base_url") or "/",
            ssl=bool(args.get("ssl")),
            verify_ssl=bool(args.get("verify_ssl")),
            http_timeout=args.get("http_timeout") or 60,
            enabled=True if args.get("enabled") is None else bool(args.get("enabled")),
            is_default=args.get("is_default"),
        )
    except ValueError as exc:
        return {"error": "invalid", "message": str(exc)}, 400
    except IntegrityError:
        session.rollback()
        return {"error": "conflict", "message": _CONFLICT_MESSAGE}, 409
    return to_safe_dict(row), 201


def update_instance(session, instance_id, args):
    arg_error = _connection_arg_error(args)
    if arg_error:
        return {"error": "invalid", "message": arg_error}, 400
    repo = ArrInstanceRepository(session)
    if repo.get(instance_id) is None:
        return {"error": "not_found"}, 404

    kwargs = {}
    for field in ("name", "ip", "port", "base_url", "ssl", "verify_ssl",
                  "http_timeout", "enabled", "is_default"):
        if args.get(field) is not None:
            kwargs[field] = args[field]
    if args.get("clear_api_key"):
        kwargs["clear_api_key"] = True
    elif args.get("api_key") is not None:
        kwargs["api_key"] = args["api_key"]

    try:
        row = repo.update(instance_id, **kwargs)
    except ValueError as exc:
        return {"error": "invalid", "message": str(exc)}, 400
    except IntegrityError:
        session.rollback()
        return {"error": "conflict", "message": _CONFLICT_MESSAGE}, 409
    return to_safe_dict(row), 200


def delete_instance(session, instance_id):
    repo = ArrInstanceRepository(session)
    try:
        ok = repo.delete(instance_id)
    except ValueError as exc:
        return {"error": "conflict", "message": str(exc)}, 409
    if not ok:
        return {"error": "not_found"}, 404
    return "", 204
