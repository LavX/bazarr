# Bazarr (LavX Fork)

<p align="center">
  <a href="https://ghcr.io/lavx/bazarr"><img src="https://img.shields.io/badge/ghcr.io-lavx%2Fbazarr-blue?style=for-the-badge&logo=docker" alt="Docker"></a>
  <a href="https://github.com/LavX/bazarr/actions/workflows/sync-upstream.yml"><img src="https://img.shields.io/github/actions/workflow/status/LavX/bazarr/sync-upstream.yml?style=for-the-badge&label=UPSTREAM%20SYNC" alt="Upstream Sync"></a>
  <a href="https://github.com/LavX/bazarr/actions/workflows/build-docker.yml"><img src="https://img.shields.io/github/actions/workflow/status/LavX/bazarr/build-docker.yml?style=for-the-badge&label=DOCKER%20BUILD" alt="Docker Build"></a>
</p>

<p align="center">
  <strong>🎬 Automated subtitle management with OpenSubtitles.org scraper & AI-powered translation</strong>
</p>

<p align="center">
  This fork of <a href="https://github.com/morpheus65535/bazarr">Bazarr</a> includes:<br/>
  • OpenSubtitles.org provider that works <strong>without VIP API credentials</strong><br/>
  • <strong>AI Subtitle Translator</strong> using OpenRouter LLMs for subtitle translation
</p>

---

## ❓ Why This Fork Exists

This fork was created after the [pull request (#3012)](https://github.com/morpheus65535/bazarr/pull/3012) to add OpenSubtitles.org web scraper support was declined by the upstream maintainer:

> *"Unfortunately, you should have asked before since we won't be merging this. It's going against our agreement with os.org/os.com. We've put them on their knees before and that's the reason why os.org is only for VIP. They don't have the horse power to support us on their legacy service. You should migrate to os.com."*

**The upstream decision is understandable** - they have agreements with OpenSubtitles and want to respect their infrastructure. However, some users:
- Don't want to pay for VIP API access
- Want to use the legacy OpenSubtitles.org which has a larger subtitle database
- Are willing to accept the limitations of web scraping

**This fork provides that option** while maintaining full compatibility with upstream Bazarr. The web scraper is rate-limited and respectful to OpenSubtitles.org's servers.

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
# Pull all images
docker pull ghcr.io/lavx/bazarr:latest
docker pull ghcr.io/lavx/opensubtitles-scraper:latest
docker pull ghcr.io/lavx/ai-subtitle-translator:latest
```

---

## 🔌 What's Different in This Fork?

| Feature | Upstream Bazarr | LavX Fork |
|---------|-----------------|-----------|
| **OpenSubtitles.org (Scraper)** | ❌ Not available | ✅ Included |
| **AI Subtitle Translator** | ❌ Not available | ✅ Included |
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

### 🤖 AI Subtitle Translator

This fork includes an **LLM-powered subtitle translator** that:

- ✅ Uses **OpenRouter API** for access to 100+ AI models (Gemini, GPT, Claude, LLaMA, Grok, etc.)
- ✅ Translates subtitles when no good match is found in your target language
- ✅ **Async job queue** for handling multiple translations
- ✅ Real-time **progress tracking** in Bazarr UI
- ✅ Configurable directly in Bazarr Settings (API key, model, temperature, concurrent jobs)
- ✅ Runs as a separate microservice for reliability

**Repository:** [github.com/LavX/ai-subtitle-translator](https://github.com/LavX/ai-subtitle-translator)

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
      - "8000:8000"
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  # AI Subtitle Translator Service (required for AI translation)
  ai-subtitle-translator:
    image: ghcr.io/lavx/ai-subtitle-translator:latest
    container_name: ai-subtitle-translator
    restart: unless-stopped
    ports:
      - "8765:8765"
    environment:
      # OpenRouter API key (can also be configured in Bazarr UI)
      - OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
      - OPENROUTER_DEFAULT_MODEL=google/gemini-2.5-flash-preview-05-20
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8765/health"]
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
      ai-subtitle-translator:
        condition: service_healthy
    ports:
      - "6767:6767"
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Europe/Budapest
      # Enable the web scraper mode (auto-enables "Use Web Scraper" in settings)
      - OPENSUBTITLES_USE_WEB_SCRAPER=true
      # Point to the scraper service (port 8000)
      - OPENSUBTITLES_SCRAPER_URL=http://opensubtitles-scraper:8000
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

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PUID` | User ID for file permissions | `1000` |
| `PGID` | Group ID for file permissions | `1000` |
| `TZ` | Timezone | `UTC` |
| `OPENSUBTITLES_USE_WEB_SCRAPER` | Enable web scraper mode | `false` |
| `OPENSUBTITLES_SCRAPER_URL` | URL of the scraper service | `http://localhost:8000` |

### Enabling the Provider

1. Go to **Settings** → **Providers**
2. Enable **"OpenSubtitles.org"** (not OpenSubtitles.com - that's the API version)
3. If `OPENSUBTITLES_USE_WEB_SCRAPER=true` is set, "Use Web Scraper" will auto-enable
4. Save and test with a manual search

### Enabling AI Translation

1. Go to **Settings** → **Subtitles** → **Translating**
2. Select **"AI Subtitle Translator"** from the Translator dropdown
3. Enter your **OpenRouter API Key** (get one at [openrouter.ai/keys](https://openrouter.ai/keys))
4. Choose your preferred **AI Model** (Gemini 2.5 Flash recommended)
5. Save and test with a manual translation

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                Docker Network                                     │
│                                                                                   │
│  ┌────────────────────────┐      ┌───────────────────────┐      ┌─────────────┐  │
│  │       Bazarr           │      │ OpenSubtitles Scraper │      │   AI Sub    │  │
│  │   (LavX Fork)          │      │     (Port 8000)       │      │ Translator  │  │
│  │                        │      │                       │      │ (Port 8765) │  │
│  │  ┌──────────────────┐  │ HTTP │  ┌─────────────────┐  │      │             │  │
│  │  │ OpenSubtitles.org│──┼──────┼──│ Search API      │  │      │ ┌─────────┐ │  │
│  │  │ Provider         │  │  API │  │ Download API    │  │      │ │Translate│ │  │
│  │  └──────────────────┘  │      │  └─────────────────┘  │      │ │  API    │ │  │
│  │                        │      │          │            │      │ │Job Queue│ │  │
│  │  ┌──────────────────┐  │ HTTP │          ▼            │      │ └────┬────┘ │  │
│  │  │ AI Subtitle      │──┼──────┼──────────────────────────────┼──────┘      │  │
│  │  │ Translator       │  │  API │  ┌─────────────────┐  │      │      │      │  │
│  │  └──────────────────┘  │      │  │ Web Scraper     │  │      │      ▼      │  │
│  │                        │      │  │opensubtitles.org│  │      │ ┌─────────┐ │  │
│  │  Port 6767 (WebUI)     │      │  └─────────────────┘  │      │ │OpenRoute│ │  │
│  └────────────────────────┘      └───────────────────────┘      │ │   API   │ │  │
│                                                                  │ └─────────┘ │  │
│                                                                  └─────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────┘
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
curl http://localhost:8000/health

# Check scraper logs
docker logs opensubtitles-scraper

# Test a search (POST request format)
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"criteria":[{"imdbid":"0111161"}]}'
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
- [AI Subtitle Translator](https://github.com/LavX/ai-subtitle-translator) - AI translator docs
- [Bazarr Wiki](https://wiki.bazarr.media) - General Bazarr documentation

---

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork this repository
2. Create a feature branch
3. Submit a pull request

For major changes, please open an issue first.

---

## 🌐 About the Maintainer

This fork is maintained by **LavX**. Explore more of my projects and services:

### 🚀 Services
- **[LavX Managed Systems](https://lavx.hu)** – Enterprise AI solutions, RAG systems, and LLMOps.
- **[LavX News](https://news.lavx.hu)** – Latest insights on AI, Open Source, and emerging tech.
- **[LMS Tools](https://tools.lavx.hu)** – 140+ free, privacy-focused online tools for developers and researchers.

### 🛠️ Open Source Projects
- **[AI Subtitle Translator](https://github.com/LavX/ai-subtitle-translator)** – LLM-powered subtitle translator using OpenRouter API.
- **[OpenSubtitles Scraper](https://github.com/LavX/opensubtitles-scraper)** – Web scraper for OpenSubtitles.org (no VIP required).
- **[JFrog to Nexus OSS](https://github.com/LavX/jfrogtonexusoss)** – Automated migration tool for repository managers.
- **[WeatherFlow](https://github.com/LavX/weatherflow)** – Multi-platform weather data forwarding (WU to Windy/Idokep).
- **[Like4Like Suite](https://github.com/LavX/Like4Like-Suite)** – Social media automation and engagement toolkit.

---

## 📄 License

- [GNU GPL v3](http://www.gnu.org/licenses/gpl.html)
- Original Bazarr Copyright 2010-2025 morpheus65535
- Fork modifications Copyright 2025 LavX

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

## Status (LavX Fork)

[![GitHub issues](https://img.shields.io/github/issues/LavX/bazarr.svg?style=flat-square)](https://github.com/LavX/bazarr/issues)
[![GitHub stars](https://img.shields.io/github/stars/LavX/bazarr.svg?style=flat-square)](https://github.com/LavX/bazarr/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/LavX/bazarr.svg?style=flat-square)](https://github.com/LavX/bazarr/network)
[![Upstream Sync](https://img.shields.io/github/actions/workflow/status/LavX/bazarr/sync-upstream.yml?style=flat-square&label=upstream%20sync)](https://github.com/LavX/bazarr/actions/workflows/sync-upstream.yml)
[![Docker Build](https://img.shields.io/github/actions/workflow/status/LavX/bazarr/build-docker.yml?style=flat-square&label=docker)](https://github.com/LavX/bazarr/actions/workflows/build-docker.yml)
## Support

For installation and configuration instructions, see upstream [wiki](https://wiki.bazarr.media).

For fork-specific issues (OpenSubtitles scraper), open an issue on [this fork](https://github.com/LavX/bazarr/issues).

For general Bazarr issues, please use the [upstream repo](https://github.com/morpheus65535/bazarr/issues).

Original Bazarr Discord: [![Discord](https://img.shields.io/badge/discord-chat-MH2e2eb.svg?style=flat-square)](https://discord.gg/MH2e2eb)

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
- AnimeSub.info
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
- OpenSubtitles.org (LavX Fork)
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