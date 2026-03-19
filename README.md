# Bazarr (LavX Fork) - "Neon Pulse"

<p align="center">
  <a href="https://ghcr.io/lavx/bazarr"><img src="https://img.shields.io/badge/ghcr.io-lavx%2Fbazarr-blue?style=for-the-badge&logo=docker" alt="Docker"></a>
  <a href="https://github.com/LavX/bazarr/releases/latest"><img src="https://img.shields.io/github/v/release/LavX/bazarr?style=for-the-badge&label=RELEASE" alt="Latest Release"></a>
  <a href="https://github.com/LavX/bazarr/actions/workflows/build-docker.yml"><img src="https://img.shields.io/github/actions/workflow/status/LavX/bazarr/build-docker.yml?style=for-the-badge&label=DOCKER%20BUILD" alt="Docker Build"></a>
</p>

<p align="center">
  <strong>Automated subtitle management with OpenSubtitles.org scraper & AI-powered translation</strong>
</p>

<p align="center">
  Built on <a href="https://www.bazarr.media">Bazarr</a>, this fork adds:<br/>
  - OpenSubtitles.org provider that works <strong>without VIP API credentials</strong> (survives the API shutdown)<br/>
  - <strong>AI Subtitle Translator</strong> using OpenRouter LLMs for high-quality subtitle translation<br/>
  - <strong>Batch translation</strong> for entire series/movie libraries<br/>
  - <strong>Advanced table filters</strong> with collapsible panels, active filter pills, and audio language filtering
</p>

---

## OpenSubtitles.org API Shutdown Notice

On **January 29, 2026**, OpenSubtitles.org [announced the final shutdown](https://forum.opensubtitles.org/viewtopic.php?t=19471) of their legacy XML-RPC API for **all third-party applications** -- both VIP and non-VIP users. The shutdown is taking effect in the coming weeks.

**This fork is not affected.** It includes a self-hosted web scraper that accesses OpenSubtitles.org directly through the website, bypassing the API entirely. As long as the OpenSubtitles.org website remains accessible ([confirmed by the site admin](https://forum.opensubtitles.org/viewtopic.php?t=19471) to stay available as read-only), subtitle search and download will continue to work.

## Why This Fork Exists

With the OpenSubtitles.org API shut down for all third-party apps (VIP included), and OpenSubtitles.com still lacking full parity with the .org library, there's a real gap for users who depend on older or less common subtitles.

This fork fills that gap with a self-hosted web scraper that accesses OpenSubtitles.org directly — rate-limited and respectful to their infrastructure. It also adds AI-powered subtitle translation and advanced UI filters not available upstream.

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
| **OpenSubtitles.org (Scraper)** | ❌ Not available | ✅ Included (API-independent) |
| **AI Subtitle Translator** | ❌ Not available | ✅ Included (OpenRouter, Gemini, Lingarr) |
| **Batch Translation** | ❌ Not available | ✅ Translate entire series/libraries |
| **Audio Language Display** | ❌ Not shown in tables | ✅ Audio languages visible in all table views |
| **Advanced Table Filters** | ❌ Basic search only | ✅ Collapsible filter panel with active filter pills |
| OpenSubtitles.org (API) | Shutting down | N/A (uses scraper instead) |
| OpenSubtitles.com (API) | ✅ Available | ✅ Available |
| Docker images | linuxserver/hotio | ghcr.io/lavx |
| Python runtime | 3.11/3.12 | 3.14 with JIT |
| Fork identification in UI | N/A | ✅ "LavX Fork - Neon Pulse" |

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

### 🔍 Advanced Table Filters

All table views (Movies, Series, Wanted Movies, Wanted Series) feature a **sophisticated filter system**:

- ✅ **Collapsible filter panel** toggled via a filter button with active filter count badge
- ✅ **Include/Exclude audio language** filters with labeled, searchable multi-select dropdowns
- ✅ **Missing subtitle language** filter (on Wanted pages)
- ✅ **Active filter pills** showing each active filter as a color-coded removable badge
- ✅ **Clear all** button to reset all filters at once
- ✅ **Search by title** with inline clear button

| Wanted Movies with filters | Series with audio filter |
|:---:|:---:|
| ![Wanted Movies Filter](/screenshot/filter-wanted-movies.png?raw=true "Wanted Movies with active filters") | ![Series Filter](/screenshot/filter-series-include.png?raw=true "Series with Include Audio filter") |

| Mass Translate with filtered selection |
|:---:|
| ![Mass Translate](/screenshot/filter-mass-translate.png?raw=true "Mass Translate dialog with filtered items") |

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
- Original Bazarr: [upstream repository](https://github.com/morpheus65535/bazarr)
- Fork modifications Copyright 2025-2026 LavX

---

<details>
<summary><h2>📜 Supported Subtitle Providers</h2></summary>

Includes all upstream providers plus fork additions:

- Addic7ed
- AnimeKalesi
- Animetosho (requires [AniDb HTTP API client](https://wiki.anidb.net/HTTP_API_Definition))
- AnimeSub.info
- Assrt
- AvistaZ, CinemaZ
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
- **OpenSubtitles.org (LavX Fork — web scraper, no API needed)**
- Podnapisi
- RegieLive
- Sous-Titres.eu
- SubX
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
- Whisper (requires [whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice))
- Wizdom
- XSubs
- Yavka.net
- YIFY Subtitles
- Zimuku

</details>