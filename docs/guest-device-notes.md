# Guest-side virtio-serial device notes

The device path exposed inside the macOS guest for a `virtserialport`
depends on the VirtIO driver package in use (most Hackintosh builds use
the Brunnerla / vfrantzen VirtIO-macOS drivers or similar).

Typical patterns to check inside the guest:

```
ls -la /dev/cu.* /dev/tty.*
```

Look for an entry that appeared after adding the `virtserialport` device,
often named something like:

```
/dev/cu.virtio-serial0
/dev/cu.virtioserialport0
```

If nothing new appears:

1. Confirm the VirtIO serial kext/driver is installed and loaded
   (`kextstat | grep -i virtio` on older macOS, or check the driver
   extension status on newer versions with System Settings > Privacy &
   Security > driver extensions).
2. Confirm the QEMU device chain is present on the host
   (`virtio-serial-pci` + `virtserialport` bound to a chardev).
3. Reboot the guest after first attaching the device — VirtIO serial
   ports are typically enumerated at boot only.

Once you identify the correct path, set it in
`guest-agent/com.homelab.telemetry-agent.plist` under
`EnvironmentVariables > TELEMETRY_DEVICE`.
