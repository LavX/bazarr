"""Private pipe worker for cancellable sports HTTP. Never import application startup."""
import base64
import json
import os
from queue import Queue
import sys
from threading import Thread

MAX_COMMAND_BYTES = 1024 * 1024
MAX_MESSAGE_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 64 * 1024


def send(message):
    encoded = json.dumps(message, separators=(',', ':')).encode('utf-8') + b'\n'
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError('HTTP worker message limit exceeded')
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def chunks(response, streaming):
    if not streaming:
        yield from response.iter_content(chunk_size=CHUNK_BYTES)
        return
    # Flush at newlines for immediate SSE delivery, and at the size limit so
    # even an unending line cannot grow the child's buffer without bound.
    pending = bytearray()
    for value in response.iter_content(chunk_size=1):
        pending.extend(value)
        if value == b'\n' or len(pending) >= CHUNK_BYTES:
            yield bytes(pending)
            pending.clear()
    if pending:
        yield bytes(pending)


def read_commands(commands):
    # EOF means the owning parent vanished. Exit even if Requests is blocked in
    # DNS/TLS/headers, where its read timeout may never expire under a slow drip.
    while True:
        command = sys.stdin.buffer.readline(MAX_COMMAND_BYTES + 1)
        if not command or len(command) > MAX_COMMAND_BYTES or not command.endswith(b'\n'):
            os._exit(0)
        commands.put(command)


def main():
    commands = Queue(maxsize=1)
    Thread(target=read_commands, args=(commands,), daemon=True).start()
    session = None
    try:
        while True:
            request = json.loads(commands.get())
            if session is None:
                # The parent supplies its resolved library paths. Isolated Python
                # startup ignores PYTHONPATH/sitecustomize and never boots the app.
                sys.path[:] = request.pop('sys_path')
                import requests
                session = requests.Session()
                session.trust_env = False
            streaming = request.pop('streaming')
            url = request.pop('url')
            request.pop('allow_redirects', None)
            if isinstance(request.get('timeout'), list):
                request['timeout'] = tuple(request['timeout'])
            try:
                with session.get(url, stream=True, allow_redirects=False, **request) as response:
                    send({'type': 'headers', 'status': response.status_code, 'headers': dict(response.headers)})
                    total = 0
                    for chunk in chunks(response, streaming):
                        total += len(chunk)
                        if not streaming and total > MAX_RESPONSE_BYTES:
                            raise ValueError('HTTP response limit exceeded')
                        send({'type': 'data', 'data': base64.b64encode(chunk).decode('ascii')})
                    send({'type': 'done'})
            except Exception:
                # Requests errors can contain credentials or URLs. Never send
                # their text or a traceback over the process boundary.
                send({'type': 'error'})
    finally:
        if session is not None:
            session.close()


if __name__ == '__main__':
    main()
