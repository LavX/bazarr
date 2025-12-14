# Bazarr (LavX Fork)

[![GitHub Container Registry](https://img.shields.io/badge/ghcr.io-lavx%2Fbazarr-blue?style=flat-square&logo=docker)](https://ghcr.io/lavx/bazarr)
[![Upstream Sync](https://img.shields.io/github/actions/workflow/status/LavX/bazarr/sync-upstream.yml?label=upstream%20sync&style=flat-square)](https://github.com/LavX/bazarr/actions/workflows/sync-upstream.yml)
[![Docker Build](https://img.shields.io/github/actions/workflow/status/LavX/bazarr/build-docker.yml?label=docker%20build&style=flat-square)](https://github.com/LavX/bazarr/actions/workflows/build-docker.yml)

This is a fork of [Bazarr](https://github.com/morpheus65535/bazarr) with **OpenSubtitles.org web scraper support** - allowing subtitle downloads from OpenSubtitles.org without requiring VIP API credentials.

## 🚀 Quick Start

```bash
# Pull the image
docker pull ghcr.io/lavx/bazarr:latest

# Or use docker-compose (recommended - includes scraper service)
git clone --recursive https://github.com/LavX/bazarr.git
cd bazarr
docker compose up -d
```

## 🔌 What's Different in This Fork?

| Feature | Upstream Bazarr | LavX Fork |
|---------|-----------------|-----------|
| OpenSubtitles.org (API) | VIP only | VIP only |
| **OpenSubtitles.org (Scraper)** | ❌ Not available | ✅ Included |
| Auto-sync with upstream | N/A | ✅ Daily |
| Docker images | linuxserver/hotio | ghcr.io/lavx |

### OpenSubtitles.org Scraper Provider

This fork includes a custom provider that uses the [OpenSubtitles Scraper](https://github.com/LavX/opensubtitles-scraper) service to fetch subtitles directly from OpenSubtitles.org without requiring API authentication.

**Note:** The scraper is a separate service included in the docker-compose setup.

## 📦 Installation

### Docker Compose (Recommended)

```yaml
services:
  opensubtitles-scraper:
    image: ghcr.io/lavx/opensubtitles-scraper:latest
    container_name: opensubtitles-scraper
    restart: unless-stopped
    ports:
      - "8765:8765"

  bazarr:
    image: ghcr.io/lavx/bazarr:latest
    container_name: bazarr
    restart: unless-stopped
    depends_on:
      - opensubtitles-scraper
    ports:
      - "6767:6767"
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Europe/Budapest
      - OPENSUBTITLES_SCRAPER_URL=http://opensubtitles-scraper:8765
    volumes:
      - ./config:/config
      - /path/to/movies:/movies
      - /path/to/tv:/tv
```

### Docker CLI

```bash
# Start scraper service first
docker run -d --name opensubtitles-scraper \
  -p 8765:8765 \
  ghcr.io/lavx/opensubtitles-scraper:latest

# Start Bazarr
docker run -d --name bazarr \
  -p 6767:6767 \
  -e PUID=1000 \
  -e PGID=1000 \
  -e TZ=Europe/Budapest \
  -e OPENSUBTITLES_SCRAPER_URL=http://opensubtitles-scraper:8765 \
  -v ./config:/config \
  -v /path/to/movies:/movies \
  -v /path/to/tv:/tv \
  ghcr.io/lavx/bazarr:latest
```

## 🔄 Upstream Synchronization

This fork automatically syncs with upstream [morpheus65535/bazarr](https://github.com/morpheus65535/bazarr) daily at 4 AM UTC. Fork-specific modifications are preserved during merges.

See [Fork Maintenance Guide](docs/FORK_MAINTENANCE.md) for details.

## 📚 Documentation

- [Fork Maintenance Guide](docs/FORK_MAINTENANCE.md) - How the fork stays synchronized
- [Upstream Wiki](https://wiki.bazarr.media) - General Bazarr documentation
- [OpenSubtitles Scraper](https://github.com/LavX/opensubtitles-scraper) - Scraper service documentation

---

# Original Bazarr README

Bazarr is a companion application to Sonarr and Radarr. It manages and downloads subtitles based on your requirements. You define your preferences by TV show or movie and Bazarr takes care of everything for you.

Be aware that Bazarr doesn't scan disk to detect series and movies: It only takes care of the series and movies that are indexed in Sonarr and Radarr.

Thanks to the folks at OpenSubtitles for their logo that was an inspiration for ours.

## Support on Paypal

At the request of some, here is a way to demonstrate your appreciation for the efforts made in the development of Bazarr:
[![Donate](https://img.shields.io/badge/Donate-PayPal-green.svg)](https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=XHHRWXT9YB7WE&source=url)

# Status

[![GitHub issues](https://img.shields.io/github/issues/morpheus65535/bazarr.svg?style=flat-square)](https://github.com/morpheus65535/bazarr/issues)
[![GitHub stars](https://img.shields.io/github/stars/morpheus65535/bazarr.svg?style=flat-square)](https://github.com/morpheus65535/bazarr/stargazers)
[![Docker Pulls](https://img.shields.io/docker/pulls/linuxserver/bazarr.svg?style=flat-square)](https://hub.docker.com/r/linuxserver/bazarr/)
[![Docker Pulls](https://img.shields.io/docker/pulls/hotio/bazarr.svg?style=flat-square)](https://hub.docker.com/r/hotio/bazarr/)
[![Discord](https://img.shields.io/badge/discord-chat-MH2e2eb.svg?style=flat-square)](https://discord.gg/MH2e2eb)

# Support

For installation and configuration instructions, see [wiki](https://wiki.bazarr.media).

You can reach us for support on [Discord](https://discord.gg/MH2e2eb).

If you find a bug, please open an issue on [Github](https://github.com/morpheus65535/bazarr/issues).

# Feature Requests

If you need something that is not already part of Bazarr, feel free to create a feature request on [Feature Upvote](http://features.bazarr.media).

## Major Features Include:

- Support for major platforms: Windows, Linux, macOS, Raspberry Pi, etc.
- Automatically add new series and episodes from Sonarr
- Automatically add new movies from Radarr
- Series or movies based configuration for subtitles languages
- Scan your existing library for internal and external subtitles and download any missing
- Keep history of what was downloaded from where and when
- Manual search so you can download subtitles on demand
- Upgrade subtitles previously downloaded when a better one is found
- Ability to delete external subtitles from disk
- Currently support 184 subtitles languages with support for forced/foreign subtitles (depending of providers)
- And a beautiful UI based on Sonarr

## Supported subtitles providers:

- Addic7ed
- AnimeKalesi
- Animetosho (requires AniDb HTTP API client described [here](https://wiki.anidb.net/HTTP_API_Definition))
- Assrt
- AvistaZ, CinemaZ (Get session cookies using method described [here](https://github.com/morpheus65535/bazarr/pull/2375#issuecomment-2057010996))
- BetaSeries
- BSplayer
- Embedded Subtitles
- Gestdown.info
- GreekSubs
- GreekSubtitles
- HDBits.org
- Hosszupuska
- Karagarga.in
- Ktuvit (Get `hashed_password` using method described [here](https://github.com/XBMCil/service.subtitles.ktuvit))
- LegendasDivx
- Legendas.net
- Napiprojekt
- Napisy24
- Nekur
- OpenSubtitles.com
- OpenSubtitles.org (VIP users only)
- Podnapisi
- RegieLive
- Sous-Titres.eu
- Subdivx
- subf2m.co
- Subs.sab.bz
- Subs4Free
- Subs4Series
- Subscene
- Subscenter
- SubsRo
- Subsunacs.net
- SubSynchro
- Subtitrari-noi.ro
- subtitri.id.lv
- Subtitulamos.tv
- Supersubtitles
- Titlovi
- Titrari.ro
- Titulky.com
- Turkcealtyazi.org
- TuSubtitulo
- TVSubtitles
- Whisper (requires [ahmetoner/whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice))
- Wizdom
- XSubs
- Yavka.net
- YIFY Subtitles
- Zimuku

## Screenshot

![Bazarr](/screenshot/bazarr-screenshot.png?raw=true "Bazarr")

### License

- [GNU GPL v3](http://www.gnu.org/licenses/gpl.html)
- Copyright 2010-2024
