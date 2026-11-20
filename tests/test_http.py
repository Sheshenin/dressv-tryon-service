from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import unittest
from io import BytesIO

from PIL import Image

from dressv.http import HTTPServer
from dressv.jobs import State
from dressv.service import Service

TOKEN = "test-bearer-token-1234567"
SESSION = "test-own-session-1234567"
OTHER = "test-other-session-12345"
KEY = "test-idempotency-1234567"
BOUNDARY = "dressv-synthetic-test"


def png(size=(32, 48)):
    with BytesIO() as output:
        Image.new("RGB", size).save(output, "PNG")
        return output.getvalue()


IMAGE = png()


def multipart(prompt="Test outfit", image=IMAGE):
    metadata = json.dumps({
        "schema_version": 1, "product_id": "mock-card",
        "prompt": prompt, "required_details": ["blue hat"],
    }).encode()
    parts = [
        ("metadata", "application/json", metadata),
        ("portrait", "image/png", image),
        ("reference", "image/png", IMAGE),
    ]
    body = bytearray()
    for name, mime, value in parts:
        body.extend(
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode()
        )
        body.extend(value + b"\r\n")
    body.extend(f"--{BOUNDARY}--\r\n".encode())
    return bytes(body)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.reviewed = threading.Event()
        self.service = Service(self.generate, self.review, ttl=1)
        self.server = HTTPServer(("127.0.0.1", 0), self.service, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.02,))
        self.thread.start()

    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()

    def generate(self, payload, deadline):
        self.started.set()
        self.release.wait(2)
        if payload.prompt == "provider error":
            raise RuntimeError("private input")
        return IMAGE

    def review(self, payload, output, deadline):
        self.reviewed.set()
        return payload.prompt != "reject"

    def request(self, method, path, *, session=SESSION, key=KEY, body=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        headers = {"Authorization": "Bearer " + TOKEN, "X-DressV-Session": session}
        if body is not None:
            headers.update({
                "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
                "Idempotency-Key": key,
            })
        try:
            conn.request(method, path, body, headers)
            response = conn.getresponse()
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            data = response.read()
            if response.getheader("Content-Type") == "application/json":
                data = json.loads(data)
            return response.status, data
        finally:
            conn.close()

    def post(self, **options):
        body = options.pop("body", multipart())
        return self.request("POST", "/v1/jobs", body=body, **options)

    def job(self, **options):
        status, data = self.post(**options)
        self.assertEqual(status, 202)
        return "/v1/jobs/" + data["job_id"]

    def wait_for(self, path, state):
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            status, data = self.request("GET", path)
            if status == 200 and data["state"] == state:
                return data
            time.sleep(0.015)
        self.fail(f"Job did not reach {state}")

    def test_lifecycle_and_idempotency(self):
        path = self.job()
        self.assertTrue(self.started.wait(0.5))
        self.assertEqual(self.request("GET", path)[1], {"state": "running"})
        self.assertEqual(self.post(), (200, {"job_id": path.rsplit("/", 1)[1]}))
        self.assertEqual(self.post(body=multipart("changed"))[0], 409)
        self.assertEqual(self.request("GET", path, session=OTHER)[0], 404)
        self.assertEqual(self.request("GET", path + "/result")[0], 409)
        self.assertEqual(self.request("DELETE", path, session=OTHER)[0], 204)
        self.release.set()
        self.wait_for(path, "ready")
        self.assertEqual(self.request("GET", path + "/result", session=OTHER)[0], 404)
        for _ in range(2):
            self.assertEqual(self.request("GET", path + "/result"), (200, IMAGE))
        for _ in range(2):
            self.assertEqual(self.request("DELETE", path)[0], 204)
        self.assertEqual(self.request("GET", path + "/result")[0], 404)
        self.assertEqual(self.post()[0], 200)

    def test_unauthorized_large_body_rejected_before_read(self):
        for authorization in ("", "Authorization: Bearer wrong-key\r\n"):
            with socket.create_connection(self.server.server_address, timeout=1) as conn:
                conn.sendall((
                    "POST /v1/jobs HTTP/1.1\r\nHost: localhost\r\n"
                    f"{authorization}Content-Length: 5000000\r\n\r\n"
                ).encode())
                with conn.makefile("rb") as response:
                    self.assertIn(b"401", response.readline())
                    self.assertIn(b"Cache-Control: no-store", response.read())

    def test_invalid_inputs(self):
        for image in (b"fake", IMAGE[:36], png((4097, 1))):
            self.assertEqual(self.post(body=multipart(image=image)), (422, {"error": "invalid_image"}))
        self.assertFalse(self.started.is_set())

    def test_queue_limits_and_cancel(self):
        paths = [self.job(key=f"test-queue-key-{index:03d}") for index in range(6)]
        self.wait_for(paths[0], "running")
        self.wait_for(paths[1], "running")
        self.assertEqual(self.request("GET", paths[2])[1], {"state": "queued"})
        self.assertEqual(self.post()[0], 429)
        self.assertEqual(self.request("DELETE", paths[0])[0], 204)
        self.assertEqual(self.post()[0], 429)
        self.assertEqual(self.request("DELETE", paths[3])[0], 204)
        self.assertEqual(self.post()[0], 202)
        self.release.set()
        self.wait_for(paths[2], "ready")
        self.assertEqual(self.request("GET", paths[0])[0], 404)
        self.assertEqual(self.request("GET", paths[3])[0], 404)

    def test_cleanup_without_more_requests(self):
        path = self.job()
        job_id = path.rsplit("/", 1)[1]
        self.assertTrue(self.started.wait(0.5))
        with self.service.store._lock:
            self.assertIn(job_id, self.service.store._jobs)
        deadline = time.monotonic() + 2.25
        while time.monotonic() < deadline:
            with self.service.store._lock:
                if job_id not in self.service.store._jobs:
                    break
            time.sleep(0.02)
        else:
            self.fail("Sweeper did not remove a job without a request")
        self.release.set()
        self.assertEqual(self.request("GET", path + "/result")[0], 404)
        self.assertFalse(self.reviewed.is_set())

    def test_failed_jobs_report_sanitized_errors(self):
        self.release.set()
        path = self.job(body=multipart("provider error"))
        self.assertEqual(self.wait_for(path, "failed")["error"], "provider_failed")
        path = self.job(key="second-idempotency-1234567", body=multipart("reject"))
        self.assertEqual(self.wait_for(path, "failed")["error"], "quality_rejected")
        self.assertEqual(self.request("GET", path + "/result")[0], 409)

    def test_new_service_has_no_previous_jobs(self):
        path = self.job()
        self.release.set()
        self.wait_for(path, "ready")
        with HTTPServer(("127.0.0.1", 0), Service(self.generate, self.review), TOKEN) as fresh:
            try:
                fresh_state, _ = fresh.service.status(SESSION, path.rsplit("/", 1)[1])
                self.assertIsNone(fresh_state)
            finally:
                fresh.service.close()


if __name__ == "__main__":
    unittest.main()
