# Bazarr (LavX Fork)

<p align="center">
  <a href="https://ghcr.io/lavx/bazarr"><img src="https://img.shields.io/badge/ghcr.io-lavx%2Fbazarr-blue?style=for-the-badge&logo=docker" alt="Docker"></a>
  <a href="https://github.com/LavX/bazarr/actions/workflows/sync-upstream.yml"><img src="https://img.shields.io/github/actions/workflow/status/LavX/bazarr/sync-upstream.yml?style=for-the-badge&label=UPSTREAM%20SYNC" alt="Upstream Sync"></a>
  <a href="https://github.com/LavX/bazarr/actions/workflows/build-docker.yml"><img src="https://img.shields.io/github/actions/workflow/status/LavX/bazarr/build-docker.yml?style=for-the-badge&label=DOCKER%20BUILD" alt="Docker Build"></a>
</p>

<p align="center">
  <strong>🎬 Automated subtitle management with OpenSubtitles.org web scraper support</strong>
</p>

<p align="center">
  This fork of <a href="https://github.com/morpheus65535/bazarr">Bazarr</a> includes a custom OpenSubtitles.org provider that works <strong>without VIP API credentials</strong> by using web scraping.
</p>

---

## 🚀 Quick Start

### Option 1: Docker Compose (Recommended)

```bash
# Clone with the scraper submodule
git clone --recursive https://github.com/LavX/bazarr.git
cd bazarr

# Configure your media paths in docker-compose.yml, then:
docker compose up -d

# Access Bazarr at http://localhost:6767
```

### Option 2: Pull Pre-built Images

```bash
# Pull both images
docker pull ghcr.io/lavx/bazarr:latest
docker pull ghcr.io/lavx/opensubtitles-scraper:latest
```

---

## 🔌 What's Different in This Fork?

| Feature | Upstream Bazarr | LavX Fork |
|---------|-----------------|-----------|
| **OpenSubtitles.org (Scraper)** | ❌ Not available | ✅ Included |
| OpenSubtitles.org (API) | VIP only | VIP only |
| OpenSubtitles.com (API) | ✅ Available | ✅ Available |
| Auto-sync with upstream | N/A | ✅ Daily at 4 AM UTC |
| Docker images | linuxserver/hotio | ghcr.io/lavx |
| Fork identification in UI | N/A | ✅ "LavX Fork" |

### 🎯 OpenSubtitles.org Scraper Provider

This fork adds a **new subtitle provider** called "OpenSubtitles.org" that:

- ✅ Works **without** API credentials or VIP subscription
- ✅ Searches by IMDB ID for accurate results
- ✅ Supports both movies and TV shows
- ✅ Provides subtitle rating and download count info
- ✅ Runs as a separate microservice for reliability

---

## 📦 Installation

### Docker Compose Setup

Create a `docker-compose.yml` file:

```yaml
services:
  # OpenSubtitles.org Scraper Service (required for the scraper provider)
  opensubtitles-scraper:
    image: ghcr.io/lavx/opensubtitles-scraper:latest
    container_name: opensubtitles-scraper
    restart: unless-stopped
    ports:
      - "8765:8765"
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://localhost:8765/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  # Bazarr with OpenSubtitles.org scraper support
  bazarr:
    image: ghcr.io/lavx/bazarr:latest
    container_name: bazarr
    restart: unless-stopped
    depends_on:
      opensubtitles-scraper:
        condition: service_healthy
    ports:
      - "6767:6767"
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Europe/Budapest
      # Point to the scraper service
      - OPENSUBTITLES_SCRAPER_URL=http://opensubtitles-scraper:8765
    volumes:
      - ./config:/config
      - /path/to/movies:/movies
      - /path/to/tv:/tv

networks:
  default:
    name: bazarr-network
```

Then run:

```bash
docker compose up -d
```

### Enabling the Provider

1. Go to **Settings** → **Providers**
2. Enable **"OpenSubtitles.org"** (not OpenSubtitles.com - that's the API version)
3. Configure the scraper URL if not using the default
4. Save and test with a manual search

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Docker Network                            │
│                                                                  │
│  ┌────────────────────────┐      ┌─────────────────────────┐    │
│  │       Bazarr           │      │  OpenSubtitles Scraper  │    │
│  │   (LavX Fork)          │      │      (Port 8765)        │    │
│  │                        │      │                         │    │
│  │  ┌──────────────────┐  │ HTTP │  ┌───────────────────┐  │    │
│  │  │ OpenSubtitles.org│──┼──────┼──│  Search API       │  │    │
│  │  │ Provider         │  │  API │  │  Download API     │  │    │
│  │  └──────────────────┘  │      │  └───────────────────┘  │    │
│  │                        │      │           │             │    │
│  │  Port 6767 (WebUI)     │      │           ▼             │    │
│  └────────────────────────┘      │  ┌───────────────────┐  │    │
│                                  │  │ Web Scraper       │  │    │
│                                  │  │ opensubtitles.org │  │    │
│                                  │  └───────────────────┘  │    │
│                                  └─────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🔄 Fork Maintenance

This fork automatically stays up-to-date with upstream:

- **Daily sync** at 4 AM UTC via GitHub Actions
- **Automatic merge** when no conflicts
- **Protected files** preserved during merges:
  - OpenSubtitles scraper provider
  - Docker configuration
  - Fork documentation
  - GitHub workflows

See [Fork Maintenance Guide](docs/FORK_MAINTENANCE.md) for technical details.

---

## 🛠️ Configuration Options

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | `1000` | User ID for file permissions |
| `PGID` | `1000` | Group ID for file permissions |
| `TZ` | `UTC` | Timezone (e.g., `Europe/Budapest`) |
| `OPENSUBTITLES_SCRAPER_URL` | `http://opensubtitles-scraper:8765` | Scraper service URL |

### Volumes

| Path | Description |
|------|-------------|
| `/config` | Bazarr configuration and database |
| `/movies` | Movies library (match your Radarr path) |
| `/tv` | TV shows library (match your Sonarr path) |

---

## 🔧 Troubleshooting

### Scraper Connection Issues

```bash
# Check if scraper is healthy
curl http://localhost:8765/health

# Check scraper logs
docker logs opensubtitles-scraper

# Test a search
curl "http://localhost:8765/search?imdb_id=tt0111161&type=movie"
```

### Common Issues

| Issue | Solution |
|-------|----------|
| "Connection refused" | Ensure scraper is running and healthy |
| "No subtitles found" | Check IMDB ID is correct, try different language |
| Provider not showing | Enable it in Settings → Providers |
| Wrong file permissions | Check PUID/PGID match your user |

---

## 📚 Documentation

- [Fork Maintenance Guide](docs/FORK_MAINTENANCE.md) - How sync works
- [OpenSubtitles Scraper](https://github.com/LavX/opensubtitles-scraper) - Scraper docs
- [Bazarr Wiki](https://wiki.bazarr.media) - General Bazarr documentation

---

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork this repository
2. Create a feature branch
3. Submit a pull request

For major changes, please open an issue first.

---

## 📄 License

- [GNU GPL v3](http://www.gnu.org/licenses/gpl.html)
- Original Bazarr Copyright 2010-2024 morpheus65535
- Fork modifications Copyright 2024 LavX

---

<details>
<summary><h2>📜 Original Bazarr README</h2></summary>

# bazarr

Bazarr is a companion application to Sonarr and Radarr. It manages and downloads subtitles based on your requirements. You define your preferences by TV show or movie and Bazarr takes care of everything for you.

Be aware that Bazarr doesn't scan disk to detect series and movies: It only takes care of the series and movies that are indexed in Sonarr and Radarr.

Thanks to the folks at OpenSubtitles for their logo that was an inspiration for ours.

## Support on Paypal

At the request of some, here is a way to demonstrate your appreciation for the efforts made in the development of Bazarr:
[![Donate](https://img.shields.io/badge/Donate-PayPal-green.svg)](https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=XHHRWXT9YB7WE&source=url)

## Status

[![GitHub issues](https://img.shields.io/github/issues/morpheus65535/bazarr.svg?style=flat-square)](https://github.com/morpheus65535/bazarr/issues)
[![GitHub stars](https://img.shields.io/github/stars/morpheus65535/bazarr.svg?style=flat-square)](https://github.com/morpheus65535/bazarr/stargazers)
[![Docker Pulls](https://img.shields.io/docker/pulls/linuxserver/bazarr.svg?style=flat-square)](https://hub.docker.com/r/linuxserver/bazarr/)
[![Docker Pulls](https://img.shields.io/docker/pulls/hotio/bazarr.svg?style=flat-square)](https://hub.docker.com/r/hotio/bazarr/)
[![Discord](https://img.shields.io/badge/discord-chat-MH2e2eb.svg?style=flat-square)](https://discord.gg/MH2e2eb)

## Support

For installation and configuration instructions, see [wiki](https://wiki.bazarr.media).

You can reach us for support on [Discord](https://discord.gg/MH2e2eb).

If you find a bug, please open an issue on [Github](https://github.com/morpheus65535/bazarr/issues).

## Feature Requests

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

</details>