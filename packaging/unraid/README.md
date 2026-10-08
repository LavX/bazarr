# Unraid Community Apps package

Prepared template, not yet submitted or tested in the Unraid UI.

## Try it locally

Copy `templates/bazarr-plus.xml` into `/boot/config/plugins/dockerMan/templates-user/` on an Unraid test machine. Open Docker > Add Container and select the template. Check the appdata path, port, user, group, and time zone before applying.

The default installation mounts only `/config`. For local libraries, use **Add another Path, Port, Variable, Label or Device**, choose Path, and add the folders you use:

| Host path example | Container path | Access |
| --- | --- | --- |
| `/mnt/user/media/movies` | `/movies` | Read/Write |
| `/mnt/user/media/tv` | `/tv` | Read/Write |
| `/mnt/user/media/sports` | `/sports` | Read/Write |

These are examples, not automatic mounts. Match the paths used by your library integrations, or configure remote path mappings. The selected UID/GID needs permission to write subtitles. Do not expose the app directly to the internet.

For migration, stop upstream Bazarr, back up its config, and select that directory instead. Never run both applications against one config. If testing side by side, use a separate config and host port. Restoring the backup is safer than assuming an older version can read an upgraded database.

FlareSolverr and the AI translator are optional, separately installed companions. See the [getting started guide](https://lavx.github.io/bazarr/guides/getting-started.html).

## Submission handoff

Publish this directory as a dedicated public template repository, with `ca_profile.xml` at its root. Before submission, add a `TemplateURL` element containing the real public raw URL of `templates/bazarr-plus.xml`. It is deliberately omitted here because the template repository has not been published. Include the project license when publishing the repository.

Run Validate and Scan in the [Community Apps submission flow](https://ca.unraid.net/submit/new), review the generated listing, then submit for moderation. No catalog submission has been made by preparing these files.

The image is pinned to `2.7.0`. For subsequent releases, update the image tag and test the new version before publishing a template update. This deliberately does not follow a moving `latest` tag.

## Validation

Checked September 28, 2026: both XML files parsed successfully and the template's required fields and config-only default passed local checks. A disposable Docker container using the exact template environment and security arguments started healthy, ran as UID 99/GID 100, and returned supervisor state `running`. After a restart, it returned `running` again and retained its generated config and database directory. The test container and its temporary volume were removed. The public icon returned HTTP 200. This is a container smoke check, not an Unraid UI test.

XML syntax and required fields can be checked locally. Native acceptance still needs installation, WebUI launch, restart persistence, a real subtitle write, migration from a backed-up config, and the Community Apps validator. Check that the configured PUID/PGID is the running application identity and that the container has only CHOWN, SETUID, and SETGID capabilities.

Sources checked September 28, 2026:

- [Repository XML format](https://ca.unraid.net/submit/help/repository-xml)
- [Supported XML fields](https://ca.unraid.net/submit/help/xml-field-reference)
- [Repository profile requirements](https://ca.unraid.net/submit/help/repository-info-xml)
- [Builder guide](https://ca.unraid.net/submit/help/builders)

Runtime settings follow this project's `docker/entrypoint.sh` and `docker-compose.yml`. The entrypoint starts as root and drops to PUID/PGID. Do not add Docker's `--user` option.
