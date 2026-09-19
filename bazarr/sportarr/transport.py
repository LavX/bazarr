"""Sports-only HTTP process ownership, absolute deadlines and private IPC."""
import base64
import codecs
import json
from pathlib import Path
import subprocess
import sys
from threading import Lock, Timer

import requests

from sportarr.transport_worker import MAX_COMMAND_BYTES, MAX_MESSAGE_BYTES, MAX_RESPONSE_BYTES

WORKER_PATH = Path(__file__).with_name('transport_worker.py')


class OwnedHTTPTransport:
    def __init__(self, setup_timeout=30):
        self.setup_timeout = setup_timeout
        self._lock = Lock()
        self._process = None
        self._closed = False

    def _start(self):
        with self._lock:
            if self._closed:
                raise ValueError('Sportarr transport stopped')
            if self._process is None:
                self._process = subprocess.Popen(
                    [sys.executable, '-I', '-S', '-u', str(WORKER_PATH)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                return self._process, True
            return self._process, False

    def _message(self, process):
        raw = process.stdout.readline(MAX_MESSAGE_BYTES + 1)
        if not raw or len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b'\n'):
            raise ValueError('Sportarr HTTP worker stopped or exceeded its message limit')
        try:
            message = json.loads(raw)
        except ValueError:
            raise ValueError('Invalid Sportarr HTTP worker message') from None
        if not isinstance(message, dict) or message.get('type') == 'error':
            raise ValueError('Sportarr HTTP request failed')
        return message

    def _open(self, url, *, streaming, timeout, **kwargs):
        process, first = self._start()
        limit = min(self.setup_timeout, timeout if isinstance(timeout, (int, float)) else sum(timeout))
        timer = Timer(limit, self.close)
        timer.daemon = True
        timer.start()
        try:
            command = {'url': url, 'streaming': streaming, 'timeout': timeout, **kwargs}
            if first:
                command['sys_path'] = [str(path) for path in sys.path]
            encoded = json.dumps(command).encode('utf-8') + b'\n'
            if len(encoded) > MAX_COMMAND_BYTES:
                raise ValueError('Sportarr request exceeded its message limit')
            process.stdin.write(encoded)
            process.stdin.flush()
            message = self._message(process)
            if message.get('type') != 'headers':
                raise ValueError('Sportarr response headers missing')
            response = requests.Response()
            response.status_code = message['status']
            response.headers = requests.structures.CaseInsensitiveDict(message['headers'])
            response.url = url
            return process, response, timer
        except Exception:
            self._finish_timer(timer)
            self.close()
            raise ValueError('Could not open Sportarr HTTP response') from None

    @staticmethod
    def _finish_timer(timer):
        timer.cancel()
        timer.join()

    def _chunks(self, process):
        while True:
            message = self._message(process)
            if message.get('type') == 'done':
                return
            if message.get('type') != 'data':
                raise ValueError('Invalid Sportarr HTTP response data')
            yield base64.b64decode(message['data'], validate=True)

    def get(self, url, **kwargs):
        timeout = min(30, kwargs.pop('timeout', 30))
        process, response, timer = self._open(url, streaming=False, timeout=timeout, **kwargs)
        try:
            content = bytearray()
            for chunk in self._chunks(process):
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError('Sportarr response exceeded its size limit')
            response._content = bytes(content)
            response._content_consumed = True
            return response
        except Exception:
            self.close()
            raise ValueError('Could not read Sportarr HTTP response') from None
        finally:
            self._finish_timer(timer)

    def stream(self, client, cursor, on_open):
        headers = client._headers() | {'Accept': 'text/event-stream'}
        if cursor is not None:
            headers['Last-Event-ID'] = cursor
        process, response, timer = self._open(
            client.base_url() + '/api/stream', streaming=True, headers=headers,
            timeout=(5, 30), verify=client.verify_ssl, allow_redirects=False)
        self._finish_timer(timer)
        if response.status_code != 200 or 'text/event-stream' not in response.headers.get('Content-Type', ''):
            raise ValueError('Sportarr stream is unavailable')
        on_open()
        decoder, pending = codecs.getincrementaldecoder('utf-8')(), ''
        for chunk in self._chunks(process):
            pending += decoder.decode(chunk)
            while '\n' in pending:
                line, pending = pending.split('\n', 1)
                if len(line) > 1024 * 1024:
                    raise ValueError('Sportarr stream line is too large')
                yield line.rstrip('\r')
            if len(pending) > 1024 * 1024:
                raise ValueError('Sportarr stream line is too large')
        if pending:
            yield pending

    def close(self):
        with self._lock:
            self._closed = True
            process = self._process
        if process is None:
            return
        # Terminate/reap before closing a pipe: a buffered reader can hold its
        # lock while DNS, TLS or incomplete response headers block in Requests.
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for pipe in (process.stdin, process.stdout):
            try:
                pipe.close()
            except (BrokenPipeError, OSError):
                pass
