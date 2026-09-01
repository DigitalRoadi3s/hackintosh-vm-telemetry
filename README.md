# hackintosh-vm-telemetry

Lightweight telemetry pipeline for a macOS (Hackintosh) guest running under
QEMU/Proxmox, using a **virtio-serial** channel instead of the network stack.
The guest periodically emits a small JSON status blob over the serial
channel; the Proxmox host reads it from the QEMU-exposed UNIX socket and logs
it locally. No guest network dependency, no SSH keys to manage, no open
listening port on the guest.

## Why this transport

- Works even if guest networking is down or firewalled.
- No inbound port on the guest — data flows one direction, guest -> host.
- Mirrors how `qemu-guest-agent` works (virtio-serial channel), but scoped to
  a small read-only telemetry payload instead of a full guest-agent protocol.

## Components

| Component | Runs on | Purpose |
|---|---|---|
| `guest-agent/telemetry-agent.sh` | macOS guest | Collects host status (uptime, load, disk, memory, IP) and writes one JSON line to the virtio-serial character device. |
| `guest-agent/com.homelab.telemetry-agent.plist` | macOS guest | `launchd` job that runs the agent on an interval. |
| `host-poller/telemetry-poller.py` | Proxmox/QEMU host | Reads JSON lines from the chardev's host-side UNIX socket and appends them to a local, size-bounded log. |
| `host-poller/telemetry-poller.service` | Proxmox/QEMU host | systemd unit to run the poller continuously. |
| `guest-agent/fake-qga.py` | macOS guest | Optional. Speaks just enough of the real QEMU Guest Agent JSON-RPC protocol (`guest-sync[-delimited]`, `guest-ping`, `guest-info`, `guest-network-get-interfaces`) on Proxmox's own guest-agent channel so the VM's IP shows up in the Proxmox web UI ("IPs" panel) and `qm agent <vmid> network-get-interfaces` works, without a real qemu-ga build. |
| `guest-agent/com.homelab.fake-qga.plist` | macOS guest | `launchd` job that keeps `fake-qga.py` running as a persistent responder. |

## Optional: real guest-agent IP display

`fake-qga.py` is separate from the telemetry pipeline above and uses Proxmox's
own guest-agent channel instead of the custom one, so Proxmox has to be told
to wire that channel: set `agent: enabled=1` on the VM (`qm set <vmid> --agent
enabled=1`), then restart the VM. Proxmox then automatically adds a second
virtio-serial device chain named `org.qemu.guest_agent.0`, independent of the
`org.homelab.telemetry` one above — the two coexist fine.

It only implements read-only status/network commands; anything else (exec,
file read/write, filesystem freeze) gets a `CommandNotFound` error, same as a
real agent would return for an unsupported command.

1. Copy `guest-agent/fake-qga.py` to `/usr/local/bin/fake-qga.py`, `chmod +x`.
2. Copy `guest-agent/com.homelab.fake-qga.plist` to `~/Library/LaunchAgents/`,
   then `launchctl load ~/Library/LaunchAgents/com.homelab.fake-qga.plist`.
3. The device path is normally `/dev/cu.org.qemu.guest_agent.0` (see
   `docs/guest-device-notes.md` for how the naming works) — set `QGA_DEVICE`
   in the plist's `EnvironmentVariables` if it differs.

Note: a per-user `LaunchAgent` only starts once that user's GUI session is
active (autologin or a console login), not at boot before anyone is logged
in. That matches how `telemetry-agent`'s LaunchAgent already behaves here.

## VM configuration (host side)

Add a virtio-serial port to the guest's QEMU args (Proxmox: via `args:` in
the VM config, or `qm set`), backed by a host-side UNIX socket:

```
-device virtio-serial-pci \
-chardev socket,path=/var/run/telemetry/<vmid>.sock,server=on,wait=off,id=telemetry0 \
-device virtserialport,chardev=telemetry0,name=org.homelab.telemetry
```

Security notes:
- The socket path is host-local (`/var/run/telemetry/`), not network-exposed.
- Restrict the socket directory to the user/group running the poller
  (`chmod 0750`, owned by a dedicated non-root service account where
  possible).
- The payload is non-sensitive host telemetry only (see schema below) — do
  not extend it to include credentials, tokens, or personal data.

## Guest setup

1. Copy `guest-agent/telemetry-agent.sh` to `/usr/local/bin/telemetry-agent.sh`
   on the macOS guest, `chmod +x`.
2. Copy `guest-agent/com.homelab.telemetry-agent.plist` to
   `~/Library/LaunchAgents/` (or `/Library/LaunchDaemons/` for a system-wide
   job), then:
   ```
   launchctl load ~/Library/LaunchAgents/com.homelab.telemetry-agent.plist
   ```
3. Inside the guest, the virtio-serial port shows up as a device path (varies
   by driver — see `docs/guest-device-notes.md`). Set `TELEMETRY_DEVICE` in
   the plist's `EnvironmentVariables` to match.

## Host setup

1. Copy `host-poller/telemetry-poller.py` to the Proxmox host, e.g.
   `/opt/telemetry/telemetry-poller.py`.
2. Copy `host-poller/telemetry-poller.service` to
   `/etc/systemd/system/telemetry-poller@.service` and enable per VM:
   ```
   systemctl enable --now telemetry-poller@<vmid>.service
   ```
3. Logs land in `/var/log/telemetry/<vmid>.jsonl` (rotated, size-bounded).

## Payload schema

```json
{
  "ts": "2026-08-27T00:00:00Z",
  "hostname": "<EXAMPLE_HOSTNAME>",
  "uptime_seconds": 12345,
  "load_avg": [1.2, 1.0, 0.9],
  "disk_free_pct": 42,
  "mem_free_mb": 2048,
  "ip_addrs": ["192.168.x.x"]
}
```

No credentials, secrets, PII, or customer data are ever included in this
payload. If you extend the schema, keep that invariant.
