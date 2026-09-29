# Bazarr+ Stack

One Compose file installs **Bazarr+, AI Subtitle Translator, and FlareSolverr**, with their connections already configured.

## Install

Save [compose.yaml](compose.yaml) in its own folder, then run:

```sh
docker compose up -d
```

Open `http://YOUR-SERVER:6767` and finish the Bazarr+ setup wizard. If another Bazarr already uses that port, run `BAZARR_PORT=6768 docker compose up -d` instead, and keep that setting in a `.env` file for future commands.

No helper URLs or shared keys need copying. The stack generates a private translator key on first start. Only Bazarr's web port is published; the companions communicate on the stack's Docker network. An initialization container exits successfully after setup. The other three containers keep running. Keep the web port on your trusted network and configure authentication before exposing it more widely.

AI translation still needs **your OpenRouter API key and a model**, entered in Bazarr's setup wizard or translator settings. Installing the translator does not provide free model usage. No provider accounts are created, and no translation runs during installation.

FlareSolverr is preconfigured for the stable catalog's compatible plugins: LegendasDivx, NapiProjekt, OpenSubtitles.org, Prijevodi Online, Subscene, Subs4Series, TurkceAltyazi, Wizdom, and Yavka. Install and enable the providers you want through Subtitle Hub. The helper does not guarantee that a provider's current challenge can be solved. Other compatible plugins can use `http://flaresolverr:8191/v1` in their advanced settings. You can change a plugin's URL later in its advanced Subtitle Hub settings.

## Media folders

Add only the folders you need under the `bazarr` service's `volumes`:

```yaml
      - /path/to/movies:/movies
      - /path/to/tv:/tv
      - /path/to/sports:/sports
```

Set `PUID`, `PGID`, and `TZ` in a `.env` file if their defaults, `1000`, `1000`, and `UTC`, do not match your server. Give that user write access to media folders so Bazarr can save subtitles. Connect Sonarr, Radarr or Sportarr as needed; the stack does not install them.

## Connect existing apps

Your apps need a reachable API address, and Bazarr needs access to their media files. These are separate requirements. An API connection does not mount a media library.

**Other containers on the same Docker host:** join Bazarr to their existing Docker network. The [optional network and media overlay](compose.media.yaml.example) keeps the companion network and adds only Bazarr to that external network. Replace its network name and host media root, save it as `compose.media.yaml`, then run:

```sh
docker compose -f compose.yaml -f compose.media.yaml up -d
```

The external network must already exist, and the target apps must belong to it. Use a service name and its **container port** in Bazarr, for example `http://sonarr:8989` or `http://radarr:7878`, plus the app's API key. Use the same approach for Sportarr, Jellyfin, Silo, Emby and Plex with their configured service names, ports and authentication. Separate networks can be added if the apps do not share one. Keep the names unique on each shared network. This follows [Docker's cross-project networking](https://docs.docker.com/compose/how-tos/networking/#connecting-multiple-compose-projects).

**Apps reached over your LAN or reverse proxy:** use their reachable server address and published port, or their HTTPS URL. Do not use `localhost`, which means the Bazarr container itself. The application still needs its API key or token. Host-networked services use the host's reachable address rather than a Docker service name.

**Files:** mount the same host media root into Bazarr, preferably at the exact container path used by the connected app. For example, if Radarr reports `/media/movies/Film/movie.mkv`, that file must exist at the same path inside Bazarr. When paths differ, configure the corresponding path mapping in Bazarr. A remote server's path does not become local through its API: mount the underlying network storage on the Docker host, then bind it into Bazarr. Do not share another app's `/config` volume or mount the Docker socket for discovery.

Add the integration in Bazarr's settings and run its connection test. Confirm a library file resolves and subtitle writes work before enabling automatic downloads. The package does not discover credentials, enable every integration, install media servers, or attach itself to existing networks automatically.

## Storage and updates

This package starts with **fresh storage**. It refuses to overwrite an existing unmanaged Bazarr config or translator data directory. Existing installations need a separate, backed-up migration, not a volume swap into this package.

Keep both named volumes, `bazarr-config` and `translator-data`, together in backups. They contain your settings, database, translator jobs and matching encryption key. Restarting the stack preserves settings changed through the UI, including a different translator connection. A missing key or a key volume that does not match the original package marker stops initialization instead of silently replacing it. Health checks do not validate later custom connections or model credentials.

`docker compose down` stops and removes the containers but keeps their volumes. Do not add `--volumes` unless you intend to permanently delete the stack's saved data. Platform uninstall dialogs may offer the same destructive option.

Images are pinned to specific versions and digests. To update, review a newer package manifest and back up both volumes first. `docker compose pull` alone does not advance pinned versions.

## Platform import

For CasaOS, import the [ready-to-import manifest](../casaos/Apps/BazarrPlusStack/docker-compose.yml) as **Bazarr+ Stack**, separately from the standalone Bazarr+ package. It uses explicit ports, user IDs and time zones because CasaOS import/update does not consistently expand Compose environment placeholders. Change its published port and `port_map` together if 6767 is occupied. The native defaults are UID/GID 1000 and UTC. For a different user, change both the Bazarr environment and the initializer's first two numeric arguments, leaving the translator's two IDs at 1000. Generic Compose users can use the `.env` settings described above. Native validation is recorded in [validation.md](validation.md); catalog listing is a separate step.
