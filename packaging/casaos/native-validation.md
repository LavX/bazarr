# Native ZimaOS validation

Date: 2026-09-28

Status: ZimaOS installation succeeded. App import and runtime checks are pending
explicit approval of the required first-run privacy-policy consent. Consent was
not accepted and was not bypassed.

## Installation

- Installed stable ZimaOS 1.7.1, x86_64, from the official
  [release page](https://github.com/IceWhaleTech/ZimaOS/releases/tag/1.7.1).
- The installer ISO was
  `zimaos-x86_64-1.7.1_installer.iso`. Its SHA-256 matched the digest shown on
  the official release asset page:
  `41ba9c6b3b9b6609f4ad30a8402b2b153036680fd5d2a9475057c846275e3372`.
  The release's separate `images-checksums.txt` covers `.img` files and does
  not list the ISO.
- Installed in an isolated UEFI KVM guest with 2 vCPU, 4 GiB RAM, and a fresh
  32 GiB virtual disk. No physical disks or host mounts were attached.
- The installer did not detect the initial virtio-backed disk. Re-presenting
  the same fresh virtual disk over SATA made it visible as the guest's only
  target. Installation completed, the ISO was ejected, and the guest rebooted
  from its installed disk.
- After reboot, the native ZimaOS gateway returned HTTP 200 on its local web
  interface.
- At cleanup, an ACPI powerdown was sent to the guest and the QEMU process
  exited. The installed virtual disk and verified installer ISO were retained
  for later authorized resumption.

## Consent gate and privacy notice

The first-run page requires a checkbox labeled “Accept Privacy Policy”. No
account step was visible yet. The linked local PDF is the IceWhale privacy
statement, last updated July 12, 2024. It says device and encrypted information
may be received or shared with service providers to provide services. It also
claims that personal information, including automatically received IP, browser,
and device characteristics, is not stored, used, or analyzed. The notice does
not describe an opt-out for this processing. No account was created and no
policy consent was given.

## Package checks not yet run

- `Apps/BazarrPlus/docker-compose.yml` was not imported or installed.
- Container UID/GID behavior, config write access, restart persistence, and
  synthetic media-path access remain untested.
- This package contains app source, not a complete generated ZimaOS v2 store.
  Native Compose import and v2 store ingestion are separate checks; neither
  was performed in this run.

Once the consent question is resolved, continue from the guest's first-run
welcome page. Do not treat the Compose import as proof of v2 catalog ingestion.
