# TrueNAS 25.10.7 native runtime validation

Validated on 2026-09-28 in a disposable TrueNAS SCALE 25.10.7 virtual machine. The [official installer](https://www.truenas.com/docs/scale/25.10/gettingstarted/install/installingscale/) ISO matched SHA-256 `54ce9441ce66966a392e28f63604ca3c2c083d0bec4db7bb5af2f74f7a007c8e`. The VM had 8 GiB RAM, a dedicated 32 GiB boot disk, and a separate 32 GiB data disk. No physical disk or existing service was attached.

## Deployment path

The prepared catalog source was rendered with its `media-host-paths.yaml` test values. The rendered Compose SHA-256 was `3c69e43503cc966609331cea6280b5234201da262b3aea80e0ad7b6400354e8d`. Five bind sources, including the config source shared by the app and permissions containers, were changed from renderer test paths to four datasets in the VM. The adapted Compose object SHA-256 was `b195bccfe5d4fe1dc042646256fc383a876c18914bb4580f2052e574cf41ce9d`. No other service setting, image, port, capability, UID/GID, or mount target was changed.

The adapted Compose was submitted through TrueNAS's native `app.create` API with `custom_app=true`. This is a native TrueNAS **custom app** installation of the package-rendered workload. It is not a catalog installation, and it does not exercise catalog ingestion or the package's default ixVolume form choice.

The data pool was created only on the VM's second disk after `boot.get_disks` identified the first disk as the boot device. QEMU did not expose distinct disk serials, so pool creation required the API's duplicate-serial override after the target disk was confirmed. Four datasets were created for `/config`, `/movies`, `/tv`, and `/sports`, then assigned to UID/GID 568.

## Observed results

| Check | Result |
| --- | --- |
| Native installation | `app.create` job 115 succeeded. `app.query` reported `RUNNING`, `custom_app=true`, one running Bazarr+ container, and the expected four dataset mounts. |
| Image and isolation | Container used `ghcr.io/lavx/bazarr:2.7.0`, local image ID `sha256:4968e42cf57a0f647fe0350bd3f34b41f3ab0e8289e0086dd930fb86d91be83f`. Runtime inspection showed a read-only root filesystem, `/tmp` tmpfs, all capabilities dropped except `CHOWN`, `SETGID`, and `SETUID`. |
| Web UI and health | The forwarded web page returned HTTP 200 with `<title>Bazarr+</title>`. The app container reported `healthy`. |
| Runtime identity | `docker top` showed the supervisor and both Bazarr+ Python processes running as UID/GID 568. The container starts as root only for its entrypoint setup, as the package specifies. |
| Writable media | UID/GID 568 inside the app container created zero-byte synthetic media files under `/movies`, `/tv`, and `/sports`. Each file appeared in its VM dataset owned by `568:568`. |
| Persistent config | Bazarr+ created `config/config.yaml` and `db/bazarr.db` in the config dataset. UID/GID 568 wrote a test marker in `/config`. Native `app.stop` job 138 and `app.start` job 141 succeeded; the container returned healthy, the UI returned HTTP 200, and the marker retained SHA-256 `0d32bc67625840eca5c9532c6c16cb4352dc6d12acdbb545f5ca4ac0aa1f05f0`. The media files also persisted. |
| Clean uninstall | Native `app.delete` job 144 succeeded with volume removal disabled. The app record and its containers were absent, and the config marker and all three synthetic media files remained in their host datasets. The web port stopped responding. The VM then shut down cleanly; its virtual disks were retained for review. |

This test establishes the packaged workload's runtime behavior under TrueNAS Apps when deployed as a custom app with host-path config storage. A catalog install, default ixVolume lifecycle, migration from an existing Bazarr database, real media scanning, and provider downloads still need separate acceptance before claiming those paths are verified.
