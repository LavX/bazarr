# Package a stable release

The Platform packages workflow prepares versioned install artifacts. It does not publish release assets, update stores, merge catalog pull requests or advance the container's `latest` tag.

## Release routine

1. Review the package templates and add a release lock under `packaging/releases/`. Keep package revisions separate from application versions. Pin exact Bazarr, translator and FlareSolverr images. Companion upgrades need connection and persistence tests.
2. Select a supported runtime profile. The initial Atlas sidecar profile covers 2.7.x only. A future plugin runtime needs an explicit profile and tested migration.
3. Run offline tests and prepare a candidate. After the application release and images exist, run the live command below.
4. Review manifests, checksums and provenance. Run native install and upgrade checks for targets being published. Old receipts do not accept changed packages.
5. Publish the approved versioned files, then update each channel through its own process. Record asset URLs, store commits or PRs, and actual listed versions.

```sh
python3 packaging/test_release.py
python3 packaging/stack/test_bootstrap.py
python3 packaging/release.py --tag v2.7.0 \
  --lock packaging/releases/2.7.0.json \
  --output /tmp/bazarr-packages-v2.7.0 --verify-live
```

Use a new output directory. Omit `--verify-live` only for offline development; that output is not publication evidence. Live checks use existing GitHub CLI authentication and registry reads. Never put credentials in locks, command arguments or receipts.

The workflow runs on a final published release or through **Actions > Platform packages > Run workflow** with a tag. It must first merge through the normal release process; a local file does not activate automation. Manual replay uses the selected workflow ref, because historical v2.7.0 predates these templates. Release events use the current default branch. Provenance identifies package source separately from application version.

Release publication and image builds are separate. Registry checks wait for a bounded interval. If images arrive later, rerun without bypassing digest or architecture checks. A missing lock fails clearly: review and add it, then replay. The [GitHub release trigger](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#release) supplies the event; the routine independently rejects drafts, prereleases and non-final tags.

Workflow artifacts expire after 30 days. They are review outputs, not permanent download links. After approval, attach versioned archives, provenance and checksum files to the existing application release without replacing older assets. Packaging corrections require a new package revision. Preserve prior files for rollback.

## Distribution routes

| Ecosystem | Package route | Publication and updates |
| --- | --- | --- |
| Compose / Portainer | Combined Compose file | Publish versioned assets; Portainer imports a file or approved Git source. Native Portainer test remains open. |
| Unraid | Standalone CA XML; combined stack through a Compose manager | Publish a licensed template repository with a real TemplateURL, run CA Validate and Scan, submit for moderation. |
| Runtipi | Standalone custom/community-store app | Publish the store layout and increment `tipi_version` on updates. Native combined-store adapter is not supplied; generic stack remains available outside the store. |
| CasaOS / ZimaOS | Standalone and combined Compose candidates | CasaOS native import is tested. Build and host a store with upstream v2 tools or submit upstream; test store ingestion on each platform. |
| TrueNAS | Standalone Custom App and community catalog candidate | Submit the catalog directory to `truenas/apps`, bump package revision on updates. Generic stack is available for combined Custom App evaluation, not yet native accepted. |

Sources: [Portainer import](https://docs.portainer.io/sts/user/docker/stacks/add), [Unraid requirements](https://ca.unraid.net/submit/help/repository-xml), [Runtipi custom stores](https://runtipi.io/docs/guides/create-your-own-app-store), [Runtipi store policy](https://github.com/runtipi/runtipi-appstore/blob/master/README.md), [CasaOS/ZimaOS stores](https://github.com/IceWhaleTech/CasaOS-AppStore/blob/main/docs/guides/third-party-store-guide.md), [TrueNAS contributions](https://github.com/truenas/apps/blob/master/CONTRIBUTIONS.md).

Report a package as listed only when the public store exposes the reviewed version. A candidate download is a different outcome.

## Acceptance for each update

- [ ] Application tag, metadata and digest agree; required architecture manifests exist.
- [ ] Companion pins and runtime profile are reviewed.
- [ ] Native install opens the UI and runs under the intended non-root identity.
- [ ] Settings and paired key state survive restart and native apply/update.
- [ ] An application-version upgrade preserves backed-up config; rollback restores its backup rather than assuming database downgrade compatibility.
- [ ] Movie, TV and sports mounts are optional and writable by the selected identity.
- [ ] API reachability and filesystem path resolution are checked separately, following the [connection guide](stack/README.md#connect-existing-apps).
- [ ] Uninstall retains data unless deletion is explicitly selected.
- [ ] Receipts identify application version, package revision, OS and architecture.
- [ ] Catalog validation, submission and listing each have evidence.

See the [initial evidence matrix](README.md). Unraid native testing needs a licensed host; ZimaOS import awaits privacy consent. Default TrueNAS ixVolume remains untested. ARM64 manifests do not establish ARM64 runtime acceptance. Health checks do not prove paid translation or a solved provider challenge.
