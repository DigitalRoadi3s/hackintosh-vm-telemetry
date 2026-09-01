#!/bin/bash
#
# telemetry-agent.sh
#
# Collects basic, non-sensitive host telemetry from a macOS guest and writes
# one JSON line to a virtio-serial character device so the Proxmox/QEMU host
# can read it without any network dependency.
#
# Security notes:
# - No credentials, tokens, or personal data are collected or emitted.
# - Output device path is read from an environment variable (set in the
#   launchd plist) rather than hardcoded, so it can be adjusted per VM
#   without editing this script.
# - Fails safely: if the device is missing or unwritable, the script logs
#   to stderr and exits non-zero instead of hanging or crashing launchd.

set -euo pipefail

# Device path is provided by the launchd plist's EnvironmentVariables.
# Falls back to a common virtio-serial guest path if unset.
TELEMETRY_DEVICE="${TELEMETRY_DEVICE:-/dev/cu.virtio-serial0}"

log_err() {
  printf '%s telemetry-agent: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" >&2
}

if [[ ! -e "${TELEMETRY_DEVICE}" ]]; then
  log_err "device not found: ${TELEMETRY_DEVICE}"
  exit 1
fi

if [[ ! -w "${TELEMETRY_DEVICE}" ]]; then
  log_err "device not writable: ${TELEMETRY_DEVICE}"
  exit 1
fi

ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
hostname_val="$(hostname -s 2>/dev/null || echo unknown)"

# uptime in seconds
boot_epoch="$(sysctl -n kern.boottime 2>/dev/null | sed -E 's/^\{ sec = ([0-9]+).*/\1/')"
now_epoch="$(date -u '+%s')"
if [[ -n "${boot_epoch}" ]]; then
  uptime_seconds=$(( now_epoch - boot_epoch ))
else
  uptime_seconds=0
fi

# load averages
load_raw="$(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}' | xargs)"
# format: "1.20 1.05 0.98" -> JSON array
load_json="[$(echo "${load_raw}" | awk '{printf "%s,%s,%s", $1, $2, $3}')]"

# disk free percentage for /
disk_free_pct="$(df -P / 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print 100-$5}')"
disk_free_pct="${disk_free_pct:-0}"

# free memory in MB (approximate, via vm_stat page counts)
mem_free_mb="$(vm_stat 2>/dev/null | awk '
  /page size of/ { page_size=$8 }
  /Pages free/ { gsub("\\.", "", $3); free=$3 }
  END { if (page_size > 0) printf "%d", (free * page_size) / 1024 / 1024; else print 0 }
')"
mem_free_mb="${mem_free_mb:-0}"

# non-loopback IPv4 addresses
ip_list="$(ifconfig 2>/dev/null | awk '/inet /{print $2}' | grep -v '^127\.' | paste -sd, -)"
if [[ -n "${ip_list}" ]]; then
  ip_json="[\"$(echo "${ip_list}" | sed 's/,/","/g')\"]"
else
  ip_json="[]"
fi

payload=$(cat <<EOF
{"ts":"${ts}","hostname":"${hostname_val}","uptime_seconds":${uptime_seconds},"load_avg":${load_json},"disk_free_pct":${disk_free_pct},"mem_free_mb":${mem_free_mb},"ip_addrs":${ip_json}}
EOF
)

if ! printf '%s\n' "${payload}" > "${TELEMETRY_DEVICE}"; then
  log_err "write to ${TELEMETRY_DEVICE} failed"
  exit 1
fi

exit 0
