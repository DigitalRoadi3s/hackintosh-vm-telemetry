#!/usr/bin/env python3
"""
fake-qga.py

Minimal QEMU Guest Agent protocol responder for a macOS guest, where no
official qemu-guest-agent build exists. Runs on the SAME virtio-serial
transport real qemu-ga uses (channel name org.qemu.guest_agent.0, wired
automatically by Proxmox when `agent: enabled=1` is set on the VM), and
implements just enough of the real JSON-RPC wire protocol for Proxmox's
"IPs" panel and `qm agent ... network-get-interfaces` to work:

    guest-sync, guest-sync-delimited, guest-ping, guest-info,
    guest-network-get-interfaces

This is deliberately not a general qemu-ga replacement: no guest-exec, no
filesystem freeze/thaw, no file read/write. Anything not listed above gets
a CommandNotFound error, matching how a real agent would answer for an
unsupported command.

Security notes:
- Read-only: the only "action" ever taken is reporting network interface
  state gathered from `ifconfig -a`. No command execution, file access, or
  credentials are ever accepted or returned.
- Input is bounded and treated as untrusted: the receive buffer is capped,
  and malformed JSON is dropped rather than crashing the daemon.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time

DEVICE = os.environ.get("QGA_DEVICE", "/dev/cu.org.qemu.guest_agent.0")
MAX_BUFFER_BYTES = 64 * 1024
RECONNECT_DELAY_SECONDS = 2

SUPPORTED_COMMANDS = [
    "guest-sync",
    "guest-sync-delimited",
    "guest-ping",
    "guest-info",
    "guest-network-get-interfaces",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ INFO fake-qga: %(message)s",
)
log = logging.getLogger("fake-qga")


def hex_netmask_to_prefix(mask_hex: str) -> int:
    return bin(int(mask_hex, 16)).count("1")


def get_network_interfaces():
    out = subprocess.run(
        ["ifconfig", "-a"], capture_output=True, text=True, check=False, timeout=5
    ).stdout
    interfaces = []
    current = None
    for line in out.splitlines():
        if line and not line[0].isspace():
            name = line.split(":", 1)[0]
            current = {
                "name": name,
                "hardware-address": "00:00:00:00:00:00",
                "ip-addresses": [],
            }
            interfaces.append(current)
            continue
        if current is None:
            continue
        stripped = line.strip()
        m = re.match(r"ether (([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})", stripped)
        if m:
            current["hardware-address"] = m.group(1).lower()
            continue
        m = re.match(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)", stripped)
        if m:
            current["ip-addresses"].append(
                {
                    "ip-address": m.group(1),
                    "ip-address-type": "ipv4",
                    "prefix": hex_netmask_to_prefix(m.group(2)),
                }
            )
            continue
        m = re.match(r"inet6 ([0-9a-fA-F:]+)(?:%\S+)? prefixlen (\d+)", stripped)
        if m:
            current["ip-addresses"].append(
                {
                    "ip-address": m.group(1),
                    "ip-address-type": "ipv6",
                    "prefix": int(m.group(2)),
                }
            )
    return interfaces


def handle(cmd: dict):
    name = cmd.get("execute")
    args = cmd.get("arguments") or {}
    if name in ("guest-sync", "guest-sync-delimited"):
        return name, {"return": args.get("id")}
    if name == "guest-ping":
        return name, {"return": {}}
    if name == "guest-info":
        return name, {
            "return": {
                "version": "0.0.0-fake-qga",
                "supported_commands": [
                    {"name": c, "enabled": True, "success-response": True}
                    for c in SUPPORTED_COMMANDS
                ],
            }
        }
    if name == "guest-network-get-interfaces":
        try:
            return name, {"return": get_network_interfaces()}
        except Exception as exc:  # noqa: BLE001 - report, don't crash the daemon
            log.warning("guest-network-get-interfaces failed: %s", exc)
            return name, {"error": {"class": "GenericError", "desc": str(exc)}}
    return name, {
        "error": {
            "class": "CommandNotFound",
            "desc": f"The command {name} has not been found",
        }
    }


def extract_messages(buf: bytes):
    """Pull complete top-level JSON objects out of buf.

    Returns (list_of_raw_json_bytes, remaining_buf). Skips sync (0xFF) and
    whitespace bytes between messages; tracks quoted-string state so braces
    inside a string value don't throw off the depth count.
    """
    messages = []
    i = 0
    n = len(buf)
    while i < n:
        if buf[i] in (0xFF, 0x0A, 0x0D, 0x20, 0x09):
            i += 1
            continue
        if buf[i:i + 1] != b"{":
            i += 1
            continue
        depth = 0
        in_string = False
        escape = False
        j = i
        closed = False
        while j < n:
            b = buf[j]
            if in_string:
                if escape:
                    escape = False
                elif b == 0x5C:  # backslash
                    escape = True
                elif b == 0x22:  # "
                    in_string = False
            else:
                if b == 0x22:
                    in_string = True
                elif b == 0x7B:  # {
                    depth += 1
                elif b == 0x7D:  # }
                    depth -= 1
                    if depth == 0:
                        messages.append(buf[i:j + 1])
                        i = j + 1
                        closed = True
                        break
            j += 1
        if not closed:
            return messages, buf[i:]
    return messages, b""


def serve(fd: int) -> None:
    buf = b""
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            time.sleep(0.05)
            continue
        buf += chunk
        if len(buf) > MAX_BUFFER_BYTES and b"{" not in buf:
            log.warning("dropping %d bytes of unparsable input", len(buf))
            buf = b""
            continue
        msgs, buf = extract_messages(buf)
        for raw in msgs:
            try:
                cmd = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("dropping malformed request: %r", raw[:200])
                continue
            if not isinstance(cmd, dict):
                continue
            name, resp = handle(cmd)
            payload = json.dumps(resp).encode()
            out = b"\xff" + payload if name == "guest-sync-delimited" else payload
            os.write(fd, out)


def main() -> int:
    while True:
        if not os.path.exists(DEVICE):
            log.warning("device not present yet: %s", DEVICE)
            time.sleep(RECONNECT_DELAY_SECONDS)
            continue
        try:
            fd = os.open(DEVICE, os.O_RDWR)
        except OSError as exc:
            log.warning("open failed: %s (retrying)", exc)
            time.sleep(RECONNECT_DELAY_SECONDS)
            continue
        try:
            serve(fd)
        except OSError as exc:
            log.warning("connection error: %s (reopening)", exc)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        time.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
