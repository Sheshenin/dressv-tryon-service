"""Internal HTTP contract. Authentication precedes image reads."""

from __future__ import annotations

import errno
import hmac
import json
import re
import socket
import ssl
import threading
import time
from email.message import Message
from email.parser import BytesHeaderParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .images import media_type
from .jobs import Payload, QueueFull, State
from .service import IdempotencyConflict, Service

_IMAGE_LIMIT = 16 * 1024 * 1024
_BODY_LIMIT = _IMAGE_LIMIT + 64 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}")
_JOB_PATH = re.compile(r"/v1/jobs/([A-Za-z0-9_-]{32})(/result)?")


class RequestError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code


def _multipart(body: bytes, content_type: str) -> Payload:
    content = Message()
    content["Content-Type"] = content_type
    boundary = content.get_boundary() or ""
    if content.get_content_type() != "multipart/form-data" or not re.fullmatch(
        r"[A-Za-z0-9'()+_,./:=?-]{1,70}", boundary
    ):
        raise RequestError(415, "invalid_content_type")
    marker = b"--" + boundary.encode("ascii")
    parts = body.split(b"\r\n" + marker, maxsplit=7)
    if not 4 <= len(parts) <= 7 or not parts[0].startswith(marker):
        raise RequestError(400, "invalid_multipart")
    if parts[-1] not in (b"--", b"--\r\n"):
        raise RequestError(400, "invalid_multipart")
    fields: dict[str, list[bytes]] = {"metadata": [], "portrait": [], "reference": []}
    image_bytes = 0
    for part in (parts[0][len(marker):], *parts[1:-1]):
        headers, separator, data = part[2:].partition(b"\r\n\r\n")
        if not part.startswith(b"\r\n") or not separator or len(headers) > 2048:
            raise RequestError(400, "invalid_multipart")
        message = BytesHeaderParser().parsebytes(headers + b"\r\n\r\n")
        name = message.get_param("name", header="Content-Disposition")
        if (
            message.defects
            or message.get_content_disposition() != "form-data"
            or message.get("Content-Transfer-Encoding")
            or not isinstance(name, str)
            or name not in fields
        ):
            raise RequestError(400, "invalid_part")
        if name != "metadata":
            image_bytes += len(data)
            if image_bytes > _IMAGE_LIMIT:
                raise RequestError(413, "image_payload_too_large")
            try:
                mime = media_type(data)
            except ValueError:
                raise RequestError(422, "invalid_image") from None
            if message.get_content_type() != mime:
                raise RequestError(422, "invalid_image")
        fields[name].append(data)

    if len(fields["metadata"]) != 1 or len(fields["portrait"]) != 1 or not 1 <= len(fields["reference"]) <= 4:
        raise RequestError(400, "invalid_part_count")
    try:
        raw_metadata = fields["metadata"][0]
        if len(raw_metadata) > 32_000:
            raise ValueError()
        meta = json.loads(raw_metadata.decode("utf-8"))
        if not isinstance(meta, dict) or meta.keys() != {
            "schema_version", "product_id", "prompt", "required_details"
        }:
            raise ValueError()
        if type(meta["schema_version"]) is not int or meta["schema_version"] != 1:
            raise ValueError()
        product, prompt, details = meta["product_id"], meta["prompt"], meta["required_details"]
        if not isinstance(product, str) or not product.strip() or len(product.encode()) > 128:
            raise ValueError()
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 24_000:
            raise ValueError()
        if not isinstance(details, list) or len(details) > 32 or any(
            not isinstance(detail, str) or not detail.strip() or len(detail.encode()) > 512
            for detail in details
        ):
            raise ValueError()
    except (KeyError, TypeError, ValueError, RecursionError):
        raise RequestError(422, "invalid_metadata") from None
    return Payload(product, prompt, tuple(details), fields["portrait"][0], tuple(fields["reference"]))


class HTTPServer(ThreadingHTTPServer):
    request_queue_size = 8

    def __init__(
        self, address: tuple[str, int], service: Service, token: str, *,
        tls: ssl.SSLContext | None = None, source: str | None = None,
    ) -> None:
        if not _TOKEN.fullmatch(token):
            raise ValueError("Invalid inter-server credential")
        self.service = service
        self.authorization = f"Bearer {token}".encode("ascii")
        self.tls = tls
        self.source = source
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(address, Handler)

    def verify_request(self, request, client_address):
        return self.source is None or client_address[0] == self.source

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            if self.tls is not None:
                self.shutdown_request(request)
                return
            request.settimeout(0.25)
            try:
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\nCache-Control: no-store\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            if self.tls is not None:
                request.settimeout(5)
                try:
                    request = self.tls.wrap_socket(request, server_side=True)
                except (OSError, ValueError):
                    self.shutdown_request(request)
                    return
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Default traceback handling can include request details.
        pass


class IPv6HTTPServer(HTTPServer):
    address_family = socket.AF_INET6


class Handler(BaseHTTPRequestHandler):
    server: HTTPServer

    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, format, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _reply(self, status: int, data: dict | bytes | None = None):
        if isinstance(data, dict):
            content_type, body = "application/json", json.dumps(data).encode()
        elif isinstance(data, bytes):
            content_type, body = "image/png", data
        else:
            content_type, body = None, b""
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _header(self, name: str) -> str:
        values = self.headers.get_all(name, [])
        return values[0] if len(values) == 1 else ""

    def _session(self) -> str:
        auth = self._header("Authorization").encode("latin-1")
        if not hmac.compare_digest(auth, self.server.authorization):
            raise RequestError(401, "unauthorized")
        session = self._header("X-DressV-Session")
        if not _TOKEN.fullmatch(session):
            raise RequestError(400, "invalid_session")
        return session

    def _read_body(self) -> bytes:
        length_header = self._header("Content-Length")
        if "Transfer-Encoding" in self.headers or not re.fullmatch(r"[0-9]{1,10}", length_header):
            raise RequestError(400, "invalid_content_length")
        length = int(length_header)
        if not 1 <= length <= _BODY_LIMIT:
            raise RequestError(413, "invalid_body_size")
        deadline = time.monotonic() + 15
        chunks = bytearray()
        while len(chunks) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RequestError(408, "request_timeout")
            self.connection.settimeout(min(5, remaining))
            try:
                chunk = self.rfile.read1(min(65536, length - len(chunks)))
            except socket.timeout:
                raise RequestError(408, "request_timeout") from None
            if not chunk:
                raise RequestError(400, "incomplete_body")
            chunks.extend(chunk)
        self.connection.settimeout(5)
        return bytes(chunks)

    def _dispatch(self):
        session = self._session()
        if self.command == "POST" and self.path == "/v1/jobs":
            key = self._header("Idempotency-Key")
            if not _TOKEN.fullmatch(key):
                raise RequestError(400, "invalid_idempotency_key")
            payload = _multipart(self._read_body(), self._header("Content-Type"))
            job_id, created = self.server.service.submit(session, key, payload)
            self._reply(202 if created else 200, {"job_id": job_id})
            return
        match = _JOB_PATH.fullmatch(self.path)
        if not match:
            raise RequestError(404, "not_found")
        job_id, result_path = match.groups()
        if self.command == "DELETE" and not result_path:
            self.server.service.discard(session, job_id)
            self._reply(204)
        elif self.command == "GET" and result_path:
            state, result = self.server.service.result(session, job_id)
            if state is None:
                raise RequestError(404, "not_found")
            if state is not State.READY or result is None:
                raise RequestError(409, "result_unavailable")
            self._reply(200, result)
        elif self.command == "GET":
            state, error = self.server.service.status(session, job_id)
            if state is None:
                raise RequestError(404, "not_found")
            body = {"state": state.value}
            if error:
                body["error"] = error
            self._reply(200, body)
        else:
            raise RequestError(405, "method_not_allowed")

    def _handle(self):
        try:
            try:
                self._dispatch()
            except RequestError as error:
                self._reply(error.status, {"error": error.code})
            except IdempotencyConflict:
                self._reply(409, {"error": "idempotency_conflict"})
            except QueueFull:
                self._reply(429, {"error": "queue_full"})
        except OSError as error:
            if error.errno not in (errno.EPIPE, errno.ECONNRESET) and not isinstance(error, socket.timeout):
                raise

    do_POST = do_GET = do_DELETE = _handle
