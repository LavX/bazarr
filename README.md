<p align="center">
  <img src="frontend/public/images/logo128.png" alt="Bazarr+ Logo" width="128">
</p>

# <p align="center">Bazarr+


  <a href="https://ghcr.io/lavx/bazarr"><img src="https://img.shields.io/badge/ghcr.io-lavx%2Fbazarr-blue?style=for-the-badge&logo=docker" alt="Docker"></a>
  <a href="https://github.com/LavX/bazarr/releases/latest"><img src="https://img.shields.io/github/v/release/LavX/bazarr?style=for-the-badge&label=RELEASE" alt="Latest Release"></a>
  <a href="https://github.com/LavX/bazarr/actions/workflows/build-docker.yml"><img src="https://img.shields.io/github/actions/workflow/status/LavX/bazarr/build-docker.yml?style=for-the-badge&label=DOCKER%20BUILD" alt="Docker Build"></a>
  <a href="https://discord.gg/WSVzzaDg"><img src="https://img.shields.io/badge/Discord-Join%20Chat-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
</p>

<p align="center">
  <strong>Enhanced subtitle management built on <a href="https://www.bazarr.media">Bazarr</a></strong>
</p>

<p align="center">
  No tracking · Provider priority · OpenSubtitles.org web scraper · AI translation via OpenRouter (300+ LLMs) · **Subtitle Editor with video preview, waveform, AI translate** · API key encryption · batch translation · mass subtitle sync · 11 bulk operations · advanced table filters · security hardening · Python 3.14 · navy + amber dark theme
</p>

---

## Switching from upstream Bazarr?

- Migration can be as simple as replacing the container image with `ghcr.io/lavx/bazarr:latest` and starting the container
- Back up your `/config` directory first
- Bazarr+ uses independent versioning starting at v2.0.0, unrelated to upstream version numbers
- Config changes made by Bazarr+ are not backwards-compatible with upstream Bazarr, so switching back requires restoring your backup
- Recommended: test with a copy of your config before committing to the switch

---

## At a Glance

| Feature | Upstream Bazarr | Bazarr+ |
|---------|-----------------|---------|
| **Provider Priority** | [Rejected](https://bazarr.featureupvote.com/suggestions/112323/provider-prioritization) (62 votes) | Dual mode: priority order with early stop, or classic simultaneous |
| **OpenSubtitles.org (Scraper)** | Not available | Self-hosted FastAPI microservice via CloudScraper |
| **AI Subtitle Translator (OpenRouter)** | Not available | 300+ LLMs + any custom model ID |
| **API Key Encryption** | Not available | AES-256-GCM encryption for keys in transit |
| **Translate from Missing Menu** | Not available | Action menu on missing subs with source language picker |
| **Batch Translation** | Not available | Translate entire series/libraries from Wanted pages |
| **Mass Subtitle Sync** | [Rejected](https://bazarr.featureupvote.com/suggestions/172013/mass-sync-all-subtitles) (249 votes) | Bulk sync from Tasks page or Mass Edit, skips already-synced |
| **Bulk Operations** | One-at-a-time only | 11 batch actions: sync, translate, OCR fixes, common fixes, remove HI, remove tags, fix uppercase, reverse RTL, scan disk, search missing, upgrade (up to 10k items) |
| **Dedicated Translator Settings** | Not available | 4-zone page with pricing, cost estimates, status panel |
| **No Tracking** | GA4 + legacy UA phone home to Google | All telemetry removed, nothing phones home |
| **Security Hardening** | MD5, no CSRF/SSRF/rate limiting | PBKDF2 (600k iter), CSRF, SSRF, brute-force, 4 more |
| **Subtitle Editor** | Not available | Full editor with video preview, waveform timeline, AI translation, ffsubsync, 8 format support, 40+ shortcuts |
| **Subtitle Viewer** | Not available | Read-only subtitle preview with SRT/VTT/ASS parsing, cue table, and format detection |
| **Audio Language Display** | Not shown in tables | Badges in all table views |
| **Advanced Table Filters** | No filters | Include/exclude audio, missing subtitle, title search |
| **Floating Save + Ctrl+S** | Not available | Sticky save button with 3-option unsaved changes modal |
| **Navy + Amber Theme** | Purple | `#121125` navy to `#fff8e1` cream, amber accents |
| OpenSubtitles.com (API) | Available | Available |
| Docker images | linuxserver.io / hotio | ghcr.io/lavx (self-built, multi-arch) |
| Python runtime | 3.8-3.13 | 3.14 |

---

## Quick Start

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

### Option 3: Run without Docker

Requires Python 3.12+ and Node.js 18+ (for building the frontend).

```bash
# Clone with submodules
git clone --recursive https://github.com/LavX/bazarr.git
cd bazarr

# Install Python dependencies
pip install -r requirements.txt

# Build the frontend
cd frontend && npm ci && npm run build && cd ..

# Run
python3 docker/supervisor.py --config ./data --port 6767
```

**System dependencies** (install via your package manager):
- `ffmpeg` (subtitle sync, video analysis)
- `mediainfo` (media file metadata)
- `unrar` (compressed subtitle extraction)

**Notes:**
- The `--config` flag sets where the database, logs, and settings are stored
- The supervisor runs a lightweight aiohttp server on the same port, serving the frontend instantly and proxying API requests to the backend. You get a startup screen with progress stages while the backend initializes, and automatic restart on crashes.
- Media paths are configured in the web UI under Settings > Sonarr/Radarr

---

### Screenshots

| Series with batch actions | Mass translate dialog |
|:---:|:---:|
| ![Series Batch Actions](screenshot/series-batch-actions.png "Series list with batch toolbar and subtitle tools") | ![Mass Translate](screenshot/mass-translate.png "Mass translate dialog with model and language selection") |

| Series detail with fanart | Subtitle viewer |
|:---:|:---:|
| ![Series Detail](screenshot/series-detail.png "Series detail page with fanart bleed and episode list") | ![Subtitle Viewer](screenshot/subtitle-viewer.png "Read-only subtitle viewer with cue table") |

| AI Translator settings |
|:---:|
| ![Translator Settings](screenshot/translator-settings-v2.png "AI Translator settings with connection, model tuning, and job queue") |

---

<details>
<summary><strong>Feature Details</strong></summary>

### OpenSubtitles.org Web Scraper
OpenSubtitles.org shut down their XML-RPC API for all third-party apps, VIP included. Bazarr+ ships a self-hosted FastAPI microservice that scrapes OpenSubtitles.org directly via CloudScraper with optional FlareSolverr fallback. It provides search, subtitle listing, and download endpoints (`/api/v1/search`, `/api/v1/subtitles`, `/api/v1/download/subtitle`) and integrates into Bazarr's provider system through a mixin class. No API key or VIP subscription needed.

### Provider Priority
Upstream Bazarr queries all subtitle providers simultaneously and picks the highest-scored result. There's no way to prefer one provider over another. This has been [requested for 6 years](https://bazarr.featureupvote.com/suggestions/112323/provider-prioritization) (62 votes), but upstream rejected it as "won't happen," calling it a "major rework" that "would take months of development."

Bazarr+ solves it with a **Provider Priority toggle** in Settings > Providers. When enabled, providers are queried sequentially in the order you've arranged them. If a provider returns subtitles meeting the minimum score, Bazarr+ stops searching and uses those results. Your preferred providers (curated community sites, specialized language sources) always get first shot. When disabled, the original behavior is preserved: all providers queried simultaneously, best score wins.

### AI Subtitle Translation via OpenRouter
Upstream has Google Translate, Gemini, and Lingarr. Bazarr+ adds **OpenRouter** as a fourth translator engine, giving access to 300+ LLMs (Claude, Gemini, GPT, LLaMA, Grok, and more) plus any custom model ID from openrouter.ai. It runs as a separate microservice with an async job queue supporting 1-5 concurrent jobs and 1-8 parallel batches. Features include:
- **Translate from the subtitle action menu**: click (...) on a missing subtitle row, pick an existing source subtitle to translate from
- **Batch translation** for entire series/movie libraries from the Wanted pages
- **Dedicated settings page** with 4 zones: engine picker, connection config, model tuning (temperature, reasoning mode, parallel batches), and a live status panel showing queue stats, job progress, token usage, cost, and speed
- **Model details** fetched live from the OpenRouter API with per-million token pricing, per-episode/movie cost estimates, context length, and prompt caching indicators
- **AES-256-GCM encryption** for API keys in transit between Bazarr and the translator service, with a Test Connection button that validates encryption and API key status before saving
- **Auto disk scan** triggers Sonarr/Radarr to rescan after translation completes

### Subtitle Editor (v2.1 LiveWire)
A full browser-based subtitle editor accessible from the subtitle action menu. No desktop software needed.

| Keyboard shortcuts | AI Translate with reference |
|:---:|:---:|
| ![Shortcuts](screenshot/editor-shortcuts.png) | ![Translate](screenshot/editor-translate.png) |

- **Video preview** with direct play, remux, or transcode fallback. Audio track switching, seekbar, playback speed, subtitle overlay
- **Waveform timeline** with draggable/resizable cue regions, click-to-seek, audio track-aware peaks
- **Editable cue table** with inline timing (scroll-wheel adjust), CPS and line-length indicators, quality markers, gap detection, bookmarks
- **AI translation** with source toggle (reference/editor cues), reference subtitle loading from disk or file import, per-line AI translate button
- **ffsubsync integration** with VAD selection, Golden-Section Search, framerate options, progress tracking in Jobs Manager
- **Text styling** buttons (italic, bold, underline, symbols) with Ctrl+I/B/U shortcuts. CPS and line length strip HTML tags
- **QC panel** with configurable presets for overlap, gap, CPS, line length, and duration checks
- **Search and Replace** with regex support across all cues
- **Timing tools**: shift all cues, linear correction (two-point fit), nudge shortcuts
- **Undo/redo** with full operation history, auto-sort by start time
- **Auto-save** to localStorage on every change (2s debounce), recovery banner on reload
- **Subtitle language switcher** in breadcrumb for quick navigation between languages
- **Format support**: SRT, VTT, ASS/SSA, SUB (MicroDVD), SMI, MPL, TXT
- **40+ keyboard shortcuts** (press `?` to see the full sheet)
- **474 tests** (424 frontend + 50 backend)

### Subtitle Viewer
Read-only subtitle preview accessible from the subtitle action menu. Supports SRT, VTT, and ASS/SSA formats with automatic format detection. Shows a cue table with timestamps and text, file size, and format badge. Useful for quickly checking subtitle content and timing without downloading.

### Advanced UI
- **Table filters** on Wanted and Library pages: include/exclude audio language (multi-select), missing subtitle language filter, title search, with active filter chips and a collapsible filter panel
- **Floating save button** with Ctrl+S/Cmd+S keyboard shortcut, visible only when settings have unsaved changes
- **Three-button unsaved changes modal**: Save & Leave, Discard, or Keep Editing (upstream only has Leave/Stay)
- **Navy + amber dark theme**: custom color palette from `#121125` (navy black) to `#fff8e1` (cream), with amber brand accents (`#e68a00` to `#b36b00`)
- **Audio language display** as blue badges in all table views

### Mass Subtitle Sync
Upstream lets you sync subtitles one at a time, or per-series via Mass Edit. But there's no way to sync your entire library at once. This has been [requested for years](https://bazarr.featureupvote.com/suggestions/172013/mass-sync-all-subtitles) (249 votes), but upstream rejected it as "won't happen," saying "Bazarr isn't a batch tool."

Bazarr+ adds two entry points for bulk sync:
- **System Tasks page**: a "Mass Sync All Subtitles" task with a Run button that syncs every subtitle in your library
- **Mass Edit pages**: a "Sync Subtitles" button for both Movies and Series editors, so you can select specific items and sync their subtitles in bulk

Both use the existing ffsubsync engine. Already-synced subtitles are skipped by default (with a force re-sync option). Configurable max offset, Golden-Section Search, and framerate correction settings.

### Bulk Operations
Select multiple movies or series from the library pages and apply operations in batch. Available from the toolbar that appears when items are selected, or from the System Tasks page for library-wide runs. Confirmation is required for operations on 100+ items. Up to 10,000 items per batch.

**Subtitle modifications** (applied to all existing subtitles of selected items):
- **Sync** -- align subtitle timing to audio using ffsubsync, with configurable max offset (1-600s), Golden-Section Search, framerate correction, and force re-sync
- **OCR Fixes** -- correct common optical character recognition errors
- **Common Fixes** -- apply standard subtitle formatting and whitespace corrections
- **Remove Hearing Impaired** -- strip `[music]`, `(doorbell rings)`, and similar HI annotations
- **Remove Style Tags** -- remove `<i>`, `<b>`, `<font>` and other formatting tags
- **Fix Uppercase** -- convert ALL CAPS subtitles to proper case
- **Reverse RTL** -- fix right-to-left punctuation for Arabic, Hebrew, and similar languages
- **Translate** -- batch translate subtitles using any configured translator engine (Google, Gemini, Lingarr, or OpenRouter with 300+ LLMs)

**Media operations** (search and scan actions for selected items):
- **Scan Disk** -- rescan selected items for on-disk subtitle files
- **Search Missing** -- search all configured providers for missing subtitles
- **Upgrade** -- replace low-scoring subtitles with better matches from providers

**Profile management:**
- **Bulk profile assignment** -- select multiple movies or series and assign a language profile to all of them at once

### No Tracking / No Telemetry
Upstream Bazarr ships two analytics systems that phone home to Google: a GA4 property (`G-3820T18GE3`) in `bazarr/utilities/analytics.py` that reports your Bazarr version, Python version, Sonarr/Radarr versions, OS, subtitle provider usage, every download action, and languages searched, plus a legacy Universal Analytics tracker (`UA-86466078-1`) in the SubZero library dependency. Bazarr+ has removed both entirely. No usage data leaves your server.

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

</details>


<details>
<summary><strong>Installation and Configuration</strong></summary>

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

  # AI Subtitle Translator Service (optional, for AI translation)
  # Configure the API key in Bazarr+ Settings > AI Translator
  ai-subtitle-translator:
    image: ghcr.io/lavx/ai-subtitle-translator:latest
    container_name: ai-subtitle-translator
    restart: unless-stopped
    ports:
      - "8765:8765"
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
      - TZ=UTC
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

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | `1000` | User ID for file permissions |
| `PGID` | `1000` | Group ID for file permissions |
| `TZ` | `UTC` | Timezone (e.g., `Europe/Budapest`) |
| `OPENSUBTITLES_SCRAPER_URL` | `http://opensubtitles-scraper:8000` | OpenSubtitles.org scraper service URL (port 8000, not 8765) |

### Volumes

| Path | Description |
|------|-------------|
| `/config` | Bazarr configuration and database |
| `/movies` | Movies library (match your Radarr path) |
| `/tv` | TV shows library (match your Sonarr path) |

### Enabling the Provider

1. Go to **Settings** > **Providers**
2. Enable **"OpenSubtitles.org"** (not OpenSubtitles.com, that's the API version)
3. Set the scraper service URL (or use the `OPENSUBTITLES_SCRAPER_URL` env var)
4. Save and test with a manual search

### Enabling AI Translation

1. Go to **Settings** > **AI Translator**
2. Select **"AI Subtitle Translator"** as the translator engine
3. Enter your **OpenRouter API Key** (get one at [openrouter.ai/keys](https://openrouter.ai/keys))
4. Choose your preferred **AI Model** (Google: Gemini 2.5 Flash Lite Preview 09-2025 recommended)
5. Save and test with a manual translation

</details>

<details>
<summary><strong>Architecture</strong></summary>

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

> **Note:** The scraper service can optionally use [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) (port 8191) to handle browser challenges. See the Docker Compose example above for the full setup.

</details>

<details>
<summary><strong>Troubleshooting</strong></summary>

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
| Provider not showing | Enable it in Settings > Providers |
| Wrong file permissions | Check PUID/PGID match your user |

</details>

<details>
<summary><strong>Supported Subtitle Providers</strong></summary>

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

<details>
<summary><strong>About the Maintainer</strong></summary>

This fork is maintained by **LavX**. Explore more projects and services:

### Services
- **[LavX Managed Systems](https://lavx.hu)** -- Enterprise AI solutions, RAG systems, and LLMOps.
- **[LavX News](https://news.lavx.hu)** -- Latest insights on AI, Open Source, and emerging tech.
- **[LMS Tools](https://tools.lavx.hu)** -- 140+ free, privacy-focused online tools for developers and researchers.

### Open Source Projects
- **[AI Subtitle Translator](https://github.com/LavX/ai-subtitle-translator)** -- LLM-powered subtitle translator using OpenRouter API.
- **[OpenSubtitles Scraper](https://github.com/LavX/opensubtitles-scraper)** -- Web scraper for OpenSubtitles.org (no VIP required).
- **[JFrog to Nexus OSS](https://github.com/LavX/jfrogtonexusoss)** -- Automated migration tool for repository managers.
- **[WeatherFlow](https://github.com/LavX/weatherflow)** -- Multi-platform weather data forwarding (WU to Windy/Idokep).
- **[Like4Like Suite](https://github.com/LavX/Like4Like-Suite)** -- Social media automation and engagement toolkit.

</details>

---

## Documentation

- [Fork Maintenance Guide](docs/FORK_MAINTENANCE.md) - How sync works
- [OpenSubtitles Scraper](https://github.com/LavX/opensubtitles-scraper) - Scraper docs
- [AI Subtitle Translator](https://github.com/LavX/ai-subtitle-translator) - AI translator docs
- [Bazarr Wiki](https://wiki.bazarr.media) - General Bazarr documentation

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

1. Fork this repository
2. Create a feature branch from `development`
3. Submit a PR targeting `development`

For major changes, please open an issue first to discuss.

## License

- [GNU GPL v3](http://www.gnu.org/licenses/gpl.html)
- Based on [Bazarr](https://github.com/morpheus65535/bazarr) by morpheus65535
- Fork modifications Copyright 2025-2026 LavX
