#!/usr/bin/env python3
"""
telemetry-poller.py

Reads JSON telemetry lines from a QEMU virtio-serial chardev UNIX socket
(host side) and appends them to a local, size-bounded log file.

Security notes:
- No credentials or secrets are read, stored, or transmitted by this script.
- The socket is expected to be host-local and access-restricted by
  filesystem permissions (see README "VM configuration" section) — this
  script does not open any network listener itself.
- Input is treated as untrusted: each line is validated as JSON with a
  bounded size before being written to the log. Malformed lines are
  dropped and logged (to stderr) rather than crashing the poller.
- Runs as an unprivileged service account where possible (see the
  accompanying systemd unit, which does not require root).
"""

import argparse
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

MAX_LINE_BYTES = 16 * 1024  # guard against unbounded/garbage input
MAX_LOG_BYTES = 10 * 1024 * 1024  # rotate at 10MB
RECONNECT_DELAY_SECONDS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s telemetry-poller: %(message)s",
)
log = logging.getLogger("telemetry-poller")


def rotate_if_needed(log_path: Path) -> None:
    try:
        if log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES:
            rotated = log_path.with_suffix(log_path.suffix + ".1")
            log_path.replace(rotated)
    except OSError as exc:
        log.warning("log rotation check failed: %s", exc)


def validate_payload(raw_line: bytes) -> dict | None:
    if len(raw_line) > MAX_LINE_BYTES:
        log.warning("dropping oversized line (%d bytes)", len(raw_line))
        return None
    try:
        text = raw_line.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        log.warning("dropping non-utf8 line")
        return None
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        log.warning("dropping malformed JSON line")
        return None
    if not isinstance(obj, dict):
        log.warning("dropping non-object JSON payload")
        return None
    return obj


def poll_once(sock_path: Path, log_path: Path) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(sock_path))
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                log.info("socket closed by peer, will reconnect")
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                payload = validate_payload(line)
                if payload is None:
                    continue
                rotate_if_needed(log_path)
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(payload, separators=(",", ":")) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socket",
        required=True,
        help="Path to the host-side virtio-serial chardev UNIX socket, "
             "e.g. /var/run/telemetry/101.sock",
    )
    parser.add_argument(
        "--log-file",
        required=True,
        help="Path to append validated JSON telemetry lines, "
             "e.g. /var/log/telemetry/101.jsonl",
    )
    args = parser.parse_args()

    sock_path = Path(args.socket)
    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)

    while True:
        if not sock_path.exists():
            log.warning("socket not present yet: %s", sock_path)
            time.sleep(RECONNECT_DELAY_SECONDS)
            continue
        try:
            poll_once(sock_path, log_path)
        except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            log.warning("connection error: %s (retrying)", exc)
        time.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
