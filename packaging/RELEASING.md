# Package a stable release

The Platform packages workflow prepares versioned install artifacts. It does not publish release assets, update stores, merge catalog pull requests or advance the container's `latest` tag.

## Release routine

1. Review the package templates. The lock for a new release is resolved automatically: after the tagged image build completes, the Platform packages workflow resolves `packaging/releases/<version>.json`, prepares, validates and exports the packages from it, and opens a lock-only pull request into `development` naming the resolved version, app image digest, companion pins and platform revisions. Review and merge that pull request. Keep package revisions separate from application versions.
2. A hand-written lock still wins. When the release lock already exists, the automation passes it through and proposes nothing. Companion upgrades need connection and persistence tests, a new runtime profile needs an explicit profile and tested migration, and a first release of a new major.minor line needs a hand-reviewed lock. The Atlas sidecar profile covers 2.7.x only.
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

The workflow runs when the Build Docker Image workflow completes, or through **Actions > Platform packages > Run workflow** with a tag. Its workflow_run lane prepares packages only when the triggering build succeeded and its head branch is a final stable tag; other completions are skipped. It must first merge through the normal release process; a local file does not activate automation. The workflow_run event only uses the copy of this workflow on the default branch, so the automation first fires for a release after a release PR carries this machinery into master. Image publication and the latest-tag advancement therefore happen before the lock pull request is reviewed, and the packages publish only after the approved lock merges. Manual replay uses the selected workflow ref, because historical v2.7.0 predates these templates, and it stays strict: it requires the checked-in lock and resolves nothing. Provenance identifies package source separately from application version.

Release publication and image builds are separate. Registry checks wait for a bounded interval, and resolution verifies the image attestation so a mutable-tag race cannot pin the wrong digest. If images arrive later, rerun without bypassing digest, architecture or attestation checks. A manual replay without a checked-in lock fails clearly: review and add it, then replay. The [workflow_run trigger](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run) supplies the event; the routine independently rejects drafts, prereleases and non-final tags.

Workflow artifacts expire after 30 days. They are review outputs, not permanent download links. After approval, attach versioned archives, provenance and checksum files to the existing application release without replacing older assets. Packaging corrections require a new package revision. Preserve prior files for rollback.

## Export store repositories

`marketplaces.py` turns one release lock into the root of a store repository. The chosen destination is `LavX/bazarr-packages` on branch `main`. That repository is prepared for, not yet created or published, and no store lists these apps yet.

```sh
python3 packaging/test_marketplaces.py
python3 packaging/marketplaces.py --tag v2.7.0 \
  --lock packaging/releases/2.7.0.json \
  --repository LavX/bazarr-packages --ref main \
  --output /tmp/bazarr-store-v2.7.0 --verify-live
```

The command runs the release preparation above, so the same lock checks, pins and `--verify-live` rules apply. Without `--verify-live` the export is an offline candidate and its README says so. The output directory must not exist. A refusal leaves no partial tree.

| Path in the export | Used by |
| --- | --- |
| `apps/bazarr-plus/` | Runtipi custom store |
| `Apps/BazarrPlus/`, `Apps/BazarrPlusStack/`, `store-config.json`, `supported-languages.json` | CasaOS / ZimaOS v2 store source |
| `templates/bazarr-plus.xml`, `ca_profile.xml` | Unraid template repository, with a TemplateURL for the chosen repository and branch |
| `stacks/bazarr-plus/` | Compose / Portainer raw file or Git source |
| `submissions/truenas/` | Directory to copy into a `truenas/apps` pull request, with its test fixtures and library license |
| `.github/workflows/store.yml`, `.github/scripts/verify_export.py` | Destination workflow that builds the CasaOS feeds and publishes them to GitHub Pages |
| `category-list.json`, `featured-apps.json`, `recommend-list.json`, `build/.gitkeep` | Minimal inputs for the legacy CasaOS v1 ZIP: one Media category, nothing featured or recommended |
| `README.md`, `EXPORT.json`, `LICENSE`, `NOTICES.md` | Install URLs, provenance and file hashes, licenses |

Compose files in the export carry an `x-bazarr-plus-package` block and the Unraid template a `<Changes>` line, so a package revision bump changes what store clients compare.

The platform packages workflow uploads a store export per candidate as a `store-exports-*` artifact, hidden `.github` files included. Pull request runs export offline. Release and manual runs export with `--verify-live`, using only the job's read-only `GITHUB_TOKEN`. The workflow never pushes the export anywhere.

### Destination workflow

`store.yml` runs in the destination repository, not here. On pull requests and manual runs it first checks the tree against `EXPORT.json` with `verify_export.py`: any changed, missing, extra or symlinked file fails the run. It then builds two artifacts with pinned upstream tooling and keeps them for 30 days:

- `casaos-v2`: the v2 feed from `IceWhaleTech/build-appstore-action` at `c9bb47fad5b32d7928a07978fad68801f52e910f`, with the feed URL from `EXPORT.json` as base URL.
- `casaos-v1`: `store/main.zip` from `build_store_v1.py` in `IceWhaleTech/CasaOS-AppStore` at `0909364b800950030e71ea82355a5969a1c08b39`. That script stages in a fixed `/tmp/appstore-v1`, so it runs in its own job.

No upstream installer or builder code is copied into the export. Build jobs only get `contents: read`, and checkouts do not keep credentials.

Publishing happens only on a manual run on `main`. The `pages` job runs `verify_export.py --publish`, which also requires `live_verified: true`, a clean packaging commit, and a repository and ref that match the run. Then it assembles the v2 feed with the v1 ZIP at `store/main.zip`. Only the `deploy` job holds `pages: write` and `id-token: write`, and it deploys through the `github-pages` environment. Builds and deploys share one concurrency queue per ref. A newer queued run replaces an older one that has not started yet.

One-time setup in the destination repository, done by the owner:

1. Settings, Pages, Build and deployment: set Source to GitHub Actions.
2. Settings, Environments: open `github-pages`, or create it if it is not listed. Limit deployment branches to `main` and add the owner under Required reviewers. Leave Prevent self-review off when the owner is the only reviewer, otherwise nobody can approve the owner's own run.

The environment name alone does not require approval. Until required reviewers are configured, a manual run on `main` that passes verification deploys straight away. With reviewers set, the `deploy` job waits for an approval in the run page, and a rejected or expired request publishes nothing.

To publish after review:

1. Create the destination repository if it does not exist, then replace the contents of its branch with a fresh live export. Review the diff and commit it there.
2. Runtipi: add the repository as a custom app store and install from it.
3. CasaOS / ZimaOS: complete the one-time setup above, check the `casaos-v2` and `casaos-v1` artifacts from the pull request or a manual run, then run Store feeds manually on `main` and approve the deployment. Add the Pages URL from the export README as a store and install from it.
4. Unraid: run CA Validate and Scan against the template repository, then submit it for moderation.
5. TrueNAS: copy `submissions/truenas/ix-dev/community/bazarr-plus` into a fork of `truenas/apps`, let the catalog tooling generate `item.yaml`, and open the pull request.

Record the destination commit, the Pages URL and each store's response. Until a store shows the reviewed version, the app is only prepared for that store.

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
