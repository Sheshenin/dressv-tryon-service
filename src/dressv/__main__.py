"""Run the internal mock server with a synthetic, generated result."""

from __future__ import annotations

import argparse
import ipaddress
import os
import signal
import ssl
import sys
import threading
from io import BytesIO
from pathlib import Path

from PIL import Image

from .http import HTTPServer, IPv6HTTPServer
from .service import Service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", required=True, action="store_true")
    parser.add_argument("--port", type=int, default=18444)
    parser.add_argument("--bind-v6", type=ipaddress.IPv6Address)
    parser.add_argument("--allow-v6", type=ipaddress.IPv6Address)
    options = parser.parse_args()
    if bool(options.bind_v6) != bool(options.allow_v6):
        parser.error("--bind-v6 and --allow-v6 must be set together")
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        sys.exit("Missing service credentials directory")
    try:
        with (Path(directory) / "wordpress-token").open(encoding="ascii") as credential:
            token = credential.read(256).strip()
    except (OSError, UnicodeError):
        sys.exit("Cannot load the inter-server credential")

    server_type, bind, tls = HTTPServer, "127.0.0.1", None
    if options.bind_v6:
        server_type, bind = IPv6HTTPServer, str(options.bind_v6)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            tls.load_cert_chain(Path(directory) / "tls-cert", Path(directory) / "tls-key")
        except (OSError, ValueError):
            sys.exit("Cannot load the Dress V TLS credentials")

    with BytesIO() as mock:
        Image.new("RGB", (64, 96), (78, 80, 128)).save(mock, "PNG")
        result = mock.getvalue()
    service = Service(lambda payload, deadline: result, lambda payload, output, deadline: True)
    try:
        with server_type(
            (bind, options.port), service, token, tls=tls,
            source=str(options.allow_v6) if options.allow_v6 else None,
        ) as server:
            stopped = threading.Event()
            signal.signal(signal.SIGTERM, lambda signum, frame: stopped.set())
            server.timeout = 0.25
            while not stopped.is_set():
                server.handle_request()
    finally:
        service.close()


if __name__ == "__main__":
    main()
