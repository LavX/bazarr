<p align="center">
  <img src="frontend/public/images/logo128.png" alt="Bazarr+ Logo" width="96">
</p>

# <p align="center">Bazarr+


  <a href="https://ghcr.io/lavx/bazarr"><img src="https://img.shields.io/badge/ghcr.io-lavx%2Fbazarr-blue?style=for-the-badge&logo=docker" alt="Docker"></a>
  <a href="https://github.com/LavX/bazarr/releases/latest"><img src="https://img.shields.io/github/v/release/LavX/bazarr?style=for-the-badge&label=RELEASE" alt="Latest Release"></a>
  <a href="https://github.com/LavX/bazarr/actions/workflows/build-docker.yml"><img src="https://img.shields.io/github/actions/workflow/status/LavX/bazarr/build-docker.yml?style=for-the-badge&label=DOCKER%20BUILD" alt="Docker Build"></a>
</p>

<p align="center">
  <strong>Enhanced subtitle management built on <a href="https://www.bazarr.media">Bazarr</a></strong>
</p>

<p align="center">
  OpenSubtitles.org web scraper · AI translation via OpenRouter (300+ LLMs) · batch translation · advanced table filters · security hardening · Python 3.14 · navy + amber dark theme
</p>

---

## Why Bazarr+?

Bazarr is great at finding subtitles. Bazarr+ takes it further with features upstream doesn't have:

### OpenSubtitles.org Web Scraper
OpenSubtitles.org shut down their XML-RPC API for all third-party apps, VIP included. Bazarr+ ships a self-hosted FastAPI microservice that scrapes OpenSubtitles.org directly via CloudScraper with optional FlareSolverr fallback. It provides search, subtitle listing, and download endpoints (`/api/v1/search`, `/api/v1/subtitles`, `/api/v1/download/subtitle`) and integrates into Bazarr's provider system through a mixin class. No API key or VIP subscription needed.

### AI Subtitle Translation via OpenRouter
Upstream has Google Translate, Gemini, and Lingarr. Bazarr+ adds **OpenRouter** as a fourth translator engine, giving access to 30+ preconfigured LLMs (Claude, Gemini, GPT, LLaMA, Grok, and more) plus any custom model ID from openrouter.ai. It runs as a separate microservice with an async job queue supporting 1-5 concurrent jobs and 1-8 parallel batches. Features include:
- **Translate from the subtitle action menu**: click (...) on a missing subtitle row, pick an existing source subtitle to translate from
- **Batch translation** for entire series/movie libraries from the Wanted pages
- **Dedicated settings page** with 4 zones: engine picker, connection config, model tuning (temperature, reasoning mode, parallel batches), and a live status panel showing queue stats, job progress, token usage, cost, and speed
- **Model details** fetched live from the OpenRouter API with per-million token pricing, per-episode/movie cost estimates, context length, and prompt caching indicators

### Advanced UI
- **Table filters** on Wanted and Library pages: include/exclude audio language (multi-select), missing subtitle language filter, title search, with active filter chips and a collapsible filter panel
- **Floating save button** with Ctrl+S/Cmd+S keyboard shortcut, visible only when settings have unsaved changes
- **Three-button unsaved changes modal**: Save & Leave, Discard, or Keep Editing (upstream only has Leave/Stay)
- **Navy + amber dark theme**: custom color palette from `#121125` (navy black) to `#fff8e1` (cream), with amber brand accents (`#e68a00` to `#b36b00`)
- **Audio language display** as blue badges in all table views

### Security Hardening
8 areas upstream doesn't address:
- **Password hashing**: PBKDF2-SHA256 with 600,000 iterations and 16-byte random salt (upstream uses plain MD5)
- **CSRF protection**: cryptographic state tokens (`secrets.token_urlsafe(32)`) on Plex OAuth with timing-safe validation
- **SSRF blocking**: DNS pinning with IP validation, blocks loopback and link-local addresses, fails closed
- **Brute-force protection**: 5 failed attempts trigger 300-second lockout per IP, tracks up to 10,000 IPs with thread-safe OrderedDict
- **Shell injection**: replaced naive character escaping with `shlex.quote()` (POSIX) and `subprocess.list2cmdline()` (Windows)
- **Filesystem sandboxing**: blocks `/proc`, `/sys`, `/dev`, `/etc`, `/root`, `/tmp` and 4 others from the filesystem browser, resolves symlinks
- **eval() removal**: replaced `eval()` in throttled provider cache with `json.loads()` to prevent arbitrary code execution
- **API key comparison**: `hmac.compare_digest()` for constant-time comparison (upstream uses Python `in` operator, which is timing-dependent)

### Python 3.14
Dockerfile uses `python:3.14-slim-bookworm`. Upstream supports Python 3.8-3.13 and relies on third-party Docker images (LinuxServer.io, hotio). Bazarr+ builds and publishes its own multi-arch image to GHCR.

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

## At a Glance

| Feature | Upstream Bazarr | Bazarr+ |
|---------|-----------------|---------|
| **OpenSubtitles.org (Scraper)** | ❌ Not available | ✅ Self-hosted FastAPI microservice via CloudScraper |
| **AI Subtitle Translator (OpenRouter)** | ❌ Not available | ✅ 30+ preconfigured LLMs + any custom model ID |
| **Translate from Missing Menu** | ❌ Not available | ✅ Action menu on missing subs with source language picker |
| **Batch Translation** | ❌ Not available | ✅ Translate entire series/libraries from Wanted pages |
| **Dedicated Translator Settings** | ❌ Not available | ✅ 4-zone page with pricing, cost estimates, status panel |
| **Security Hardening** | MD5, no CSRF/SSRF/rate limiting | ✅ PBKDF2 (600k iter), CSRF, SSRF, brute-force, 4 more |
| **Audio Language Display** | ❌ Not shown in tables | ✅ Badges in all table views |
| **Advanced Table Filters** | ❌ No filters | ✅ Include/exclude audio, missing subtitle, title search |
| **Floating Save + Ctrl+S** | ❌ Not available | ✅ Sticky save button with 3-option unsaved changes modal |
| **Navy + Amber Theme** | Purple | ✅ `#121125` navy to `#fff8e1` cream, amber accents |
| OpenSubtitles.com (API) | ✅ Available | ✅ Available |
| Docker images | linuxserver.io / hotio | ghcr.io/lavx (self-built, multi-arch) |
| Python runtime | 3.8-3.13 | 3.14 |

| Series overview | Series detail with translate menu |
|:---:|:---:|
| ![Series Overview](/screenshot/series-overview.png?raw=true "Series overview with audio badges") | ![Translate Menu](/screenshot/series-translate-menu.png?raw=true "Translate from subtitle action menu") |

| AI Translator settings with live status | Mass Translate dialog |
|:---:|:---:|
| ![Translator Settings](/screenshot/translator-settings.png?raw=true "AI Translator settings with job queue") | ![Mass Translate](/screenshot/mass-translate-dialog.png?raw=true "Mass Translate with model and language selection") |

| Wanted page with advanced filters |
|:---:|
| ![Wanted Filters](/screenshot/wanted-filters.png?raw=true "Wanted page with include/exclude audio and subtitle filters") |

---

## 📦 Installation

### Docker Compose Setup

Create a `docker-compose.yml` file:

```yaml
services:
  # FlareSolverr - Handles browser challenges for web scraping
  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    container_name: flaresolverr
    restart: unless-stopped
    ports:
      - "8191:8191"
    environment:
      - LOG_LEVEL=info

  opensubtitles-scraper:
    image: ghcr.io/lavx/opensubtitles-scraper:latest
    container_name: opensubtitles-scraper
    restart: unless-stopped
    depends_on:
      - flaresolverr
    ports:
      - "8000:8000"
    environment:
      - FLARESOLVERR_URL=http://flaresolverr:8191/v1
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
      - OPENROUTER_DEFAULT_MODEL=google/gemini-2.5-flash-lite-preview-09-2025
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8765/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  # Bazarr+
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
| `OPENSUBTITLES_USE_WEB_SCRAPER` | Enable web scraper mode | `true` |
| `OPENSUBTITLES_SCRAPER_URL` | URL of the scraper service | `http://localhost:8000` |

### Enabling the Provider

1. Go to **Settings** → **Providers**
2. Enable **"OpenSubtitles.org"** (not OpenSubtitles.com - that's the API version)
3. If `OPENSUBTITLES_USE_WEB_SCRAPER=true` is set, "Use Web Scraper" will auto-enable
4. Save and test with a manual search

### Enabling AI Translation

1. Go to **Settings** → **AI Translator**
2. Select **"AI Subtitle Translator"** as the translator engine
3. Enter your **OpenRouter API Key** (get one at [openrouter.ai/keys](https://openrouter.ai/keys))
4. Choose your preferred **AI Model** (Google: Gemini 2.5 Flash Lite Preview 09-2025 recommended)
5. Save and test with a manual translation

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                Docker Network                                     │
│                                                                                   │
│  ┌────────────────────────┐      ┌───────────────────────┐      ┌─────────────┐  │
│  │       Bazarr           │      │ OpenSubtitles Scraper │      │   AI Sub    │  │
│  │    (Bazarr+)            │      │     (Port 8000)       │      │ Translator  │  │
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

> **Note:** The scraper service uses [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) (port 8191) to handle browser challenges. See the Docker Compose example above for the full setup.

---

## 🛠️ Configuration Options

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | `1000` | User ID for file permissions |
| `PGID` | `1000` | Group ID for file permissions |
| `TZ` | `UTC` | Timezone (e.g., `Europe/Budapest`) |
| `OPENSUBTITLES_USE_WEB_SCRAPER` | `true` | Enable the OpenSubtitles.org web scraper provider |
| `OPENSUBTITLES_SCRAPER_URL` | `http://opensubtitles-scraper:8000` | Scraper service URL (port 8000, not 8765) |

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

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

1. Fork this repository
2. Create a feature branch from `development`
3. Submit a PR targeting `development`

For major changes, please open an issue first to discuss.

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
- **OpenSubtitles.org (Bazarr+ web scraper, no API needed)**
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
