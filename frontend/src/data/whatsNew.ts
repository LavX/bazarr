import type { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import {
  faClockRotateLeft,
  faDatabase,
  faDownload,
  faFileZipper,
  faLayerGroup,
  faScaleBalanced,
  faServer,
  faShieldHalved,
  faSliders,
  faStore,
  faTowerBroadcast,
  faWandMagicSparkles,
} from "@fortawesome/free-solid-svg-icons";

export interface WhatsNewSlide {
  /** Short headline for the change. */
  title: string;
  /** One to three lines describing it. */
  body: string;
  /** Optional imported asset URL; takes priority over `icon`. */
  image?: string;
  /** Optional FontAwesome icon shown when there is no image. */
  icon?: IconDefinition;
  /** Optional deep-link to the relevant page ("Take me there"). */
  cta?: { label: string; to: string };
}

/**
 * The release being announced. The maintainer bumps this (and adds an entry below) when
 * cutting a release. Kept as an explicit token so the wizard never has to parse the
 * fork's `version + YYMMDD` runtime string.
 */
export const latestWhatsNewVersion = "2.6.2";

// v2.6.0 feature slides; v2.6.1 (a patch on the same line) leads with its
// fix and keeps the whole Clockwork tour behind it.
const clockworkSlides: WhatsNewSlide[] = [
  {
    title: "Download your subtitles, single files or whole seasons",
    body: "Every subtitle menu now has a Download action, including synced and combined outputs. The series and movie pages can also hand you one zip of everything on disk, filtered by season and language, from the new Download button next to Upload.",
    icon: faDownload,
    cta: { label: "Open your series", to: "/series" },
  },
  {
    title: "Coming from upstream Bazarr? It just starts now",
    body: "Pointing Bazarr+ at a config directory created by upstream Bazarr used to crash on boot in a restart loop, because the two projects' migration histories diverged. Any upstream database is now adopted on first start, whatever revision it came from, with your subtitle lists preserved.",
    icon: faDatabase,
  },
  {
    title: "Movie edition scores are honest now, and some will drop",
    body: "Subtitles used to get credit for matching a movie's edition (Extended, Director's Cut) even when they named no edition at all, worth up to 30 points. That is fixed. If edition-tagged movies stop getting subtitles, your minimum score is now being applied to an honest number: lower it a notch.",
    icon: faScaleBalanced,
    cta: { label: "Open Subtitles settings", to: "/settings/subtitles" },
  },
  {
    title: "Sync rejects a bad result instead of reporting success",
    body: 'Maximum offset used to bound ffsubsync\'s search, so a subtitle minutes out of sync could come back "synced" and overwrite a good file. It is now an acceptance threshold: a result beyond it is rejected and the next engine gets its turn. Expect more honest failures, and files already ruined stay ruined.',
    icon: faClockRotateLeft,
    cta: { label: "Open Subtitles settings", to: "/settings/subtitles" },
  },
  {
    title: "Mass translate can use an embedded track",
    body: 'If a release only carries its English subtitles inside the video container, mass translate can now extract and translate them. Enable "Treat Embedded Subtitles as Downloaded" so embedded tracks are indexed, then pick the source language as usual: when no external source file exists, the embedded track is used automatically. Each variant is handled separately, so a normal and a hearing-impaired track produce their own outputs.',
    icon: faWandMagicSparkles,
    cta: { label: "Open Subtitles settings", to: "/settings/subtitles" },
  },
  {
    title: "Weight one provider up or down",
    body: "Each provider can carry a score modifier from -100% to +100%, applied before the minimum-score check. It is a percentage of the maximum score rather than raw points, so 25% on an episode is worth roughly 90. Built for keeping something like WhisperAI as a genuine last resort without lowering the bar for everyone else.",
    icon: faSliders,
    cta: { label: "Open Providers", to: "/settings/providers" },
  },
  {
    title: "Four dead providers were removed",
    body: "Hosszupuska, Podnapisi, SubsCenter and XSubs no longer work and are gone. On first start they leave your enabled list and their leftover settings, including any saved password, are deleted from the config so old cleartext credentials cannot linger.",
    icon: faStore,
    cta: { label: "Open Providers", to: "/settings/providers" },
  },
  {
    title: "CaptchaAI can solve your captchas",
    body: "CaptchaAI joins Anti-Captcha and Death by Captcha as a third anti-captcha vendor, using its flat-rate 2Captcha-compatible API for the providers that hit a reCAPTCHA on login. The API keys for both key-based vendors are now encrypted at rest like every other credential.",
    icon: faShieldHalved,
    cta: { label: "Open Providers", to: "/settings/providers" },
  },
];

export const whatsNew: Record<string, WhatsNewSlide[]> = {
  "2.6.2": [
    {
      title: "Uploaded subtitles appear before sync finishes",
      body: "A saved upload is listed immediately while automatic sync runs as a separate job. Failed or cancelled sync leaves the uploaded subtitle available, and older sync work cannot replace a newer upload.",
      icon: faDownload,
      cta: { label: "Open your series", to: "/series" },
    },
    {
      title: "One upload dialog for each batch",
      body: "Dropping files into an open upload dialog keeps the existing selection without opening another dialog, including while an archive is expanding. Closing once dismisses it.",
      icon: faFileZipper,
      cta: { label: "Open your movies", to: "/movies" },
    },
    {
      title: "Choose how OpenRouter routes your translations",
      body: "Pick fastest, cheapest, lowest latency or OpenRouter's default. Typed :nitro and :floor shortcuts move into the routing selector when editing finishes, keeping model details and the saved choice consistent. Full shortcut support needs translator service 1.3.4.",
      icon: faSliders,
      cta: { label: "Open Translator settings", to: "/settings/translator" },
    },
    {
      title: "Keyboard saves keep your latest settings edit",
      body: "When the Save bar is visible, Enter and Ctrl+S or Cmd+S now include the latest text in the focused field, including fields that finish editing when focus moves away.",
      icon: faSliders,
      cta: { label: "Open Translator settings", to: "/settings/translator" },
    },
    {
      title: "History keeps the right server",
      body: "Downloads keep their Sonarr or Radarr owner even if the media lookup fails, so a missing lookup no longer prevents the history entry from being saved.",
      icon: faClockRotateLeft,
      cta: { label: "Open History", to: "/history/series" },
    },
    {
      title: "Disk scans keep sync outputs with their video",
      body: "Keep all still stores one file per sync engine. Disk scans now keep those outputs with the owning video, including existing language-tagged files after a single-language naming change.",
      icon: faSliders,
      cta: { label: "Open Subtitles settings", to: "/settings/subtitles" },
    },
    {
      title: "Movie translation jobs show the movie title",
      body: "Movie translations now show the movie's name in the job list, and Gemini receives its overview. Starting a translation from a media page also keeps title and overview lookups with that media's Sonarr or Radarr server.",
      icon: faWandMagicSparkles,
      cta: { label: "Open your movies", to: "/movies" },
    },
    {
      title: "Recover useful OpenRouter translation results",
      body: "Usable partial OpenRouter translations are saved and marked incomplete. Missing translations retain their source text, so review incomplete files. Empty or invalid results still fail, and failed saves preserve the previous subtitle.",
      icon: faWandMagicSparkles,
      cta: { label: "Open Translator settings", to: "/settings/translator" },
    },
    {
      title: "Long OpenRouter translation jobs can finish",
      body: "OpenRouter translation jobs can keep running for up to 12 hours. Ten minutes without a successful status response still stops the wait.",
      icon: faClockRotateLeft,
      cta: { label: "Open Translator settings", to: "/settings/translator" },
    },
    {
      title: "Translation and edits respect subtitle folders",
      body: "Existing subtitles stay in their current location. New translated or modified subtitles use your configured subtitle folder, including relative and absolute custom folders.",
      icon: faFileZipper,
      cta: { label: "Open Subtitles settings", to: "/settings/subtitles" },
    },
    {
      title: "Cancelled Gemini jobs stop retrying",
      body: "Cancelling a Gemini translation no longer retries the request. The existing subtitle stays in place, and temporary progress files are cleaned up.",
      icon: faWandMagicSparkles,
      cta: { label: "Open Translator settings", to: "/settings/translator" },
    },
    {
      title: "Updated translation model choices",
      body: "Four known retired Gemini IDs in OpenRouter settings migrate to replacements. Other saved IDs stay unchanged. Removed Grok menu suggestions are not migrated automatically.",
      icon: faWandMagicSparkles,
      cta: { label: "Open Translator settings", to: "/settings/translator" },
    },
    {
      title: "One unusable archive no longer stops the search",
      body: "If a catalog-provider archive has no suitable subtitle, Bazarr tries the next candidate without disabling that provider. The search also continues when only one successful subtitle is requested.",
      icon: faDownload,
      cta: { label: "Open Subtitle Hub", to: "/subtitle-hub" },
    },
    {
      title: "Provider errors keep their meaning",
      body: "Provider Hub now preserves quota, login, rate-limit and service errors across its workers, so Bazarr applies the matching pause instead of treating them all as a generic failure.",
      icon: faStore,
      cta: { label: "Open Subtitle Hub", to: "/subtitle-hub" },
    },
    {
      title: "See which server owns a search result",
      body: "When you have multiple Sonarr or multiple Radarr instances, global search shows the owning instance beside that media type\u2019s results.",
      icon: faServer,
      cta: { label: "Open your series", to: "/series" },
    },
    {
      title: "Fewer misleading translation messages",
      body: "Automatic translation checks whether it is enabled and eligible before reporting that a source score is too low.",
      icon: faWandMagicSparkles,
      cta: { label: "Open Translator settings", to: "/settings/translator" },
    },
    {
      title: "Dependency checks match installed versions",
      body: "Source installs no longer request repeated repairs for dependencies that already satisfy the installation requirements. This patch also updates filename parsing, database access, shared UI components and security dependencies.",
      icon: faShieldHalved,
      cta: { label: "Open System Status", to: "/system/status" },
    },
    {
      title: "Provider fixes are delivered separately",
      body: "Reviewed OpenSubtitles.org, OpenSubtitles.com, Titlovi and SubDL updates are in catalog beta. Stable availability follows promotion to main; update the bundles in Subtitle Hub when available.",
      icon: faStore,
      cta: { label: "Open Subtitle Hub", to: "/subtitle-hub" },
    },
  ],
  "2.6.1": [
    {
      title: "History shows your events again",
      body: 'On a large library, "Treat Embedded Subtitles as Downloaded" buried your downloads and upgrades under thousands of Embedded Source records and made History and Wanted pages crawl. Those records are now hidden by default (a switch brings them back), and new database indexes make both pages fast at any size.',
      icon: faClockRotateLeft,
      cta: { label: "Open History", to: "/history/series" },
    },
    ...clockworkSlides,
  ],
  "2.6.0": clockworkSlides,
  "2.5.2": [
    {
      title: "WhisperAI timeouts no longer cut off at 30 seconds",
      body: "WhisperAI transcriptions used to be killed after 30 seconds no matter what response or transcription timeouts you set. Your configured timeouts now drive how long the provider is given, so long jobs run to completion.",
      icon: faWandMagicSparkles,
      cta: { label: "Open Providers", to: "/settings/providers" },
    },
    {
      title: "Configurable Provider Hub worker timeout",
      body: "A new Default worker timeout setting under General > Provider Hub sets the fallback deadline for Hub plugins that do not define their own. Plugins like WhisperAI that declare a longer timeout raise it above this value.",
      icon: faSliders,
      cta: { label: "Open General settings", to: "/settings/general" },
    },
  ],
  "2.5.1": [
    {
      title: "Cleaner multi-server first-run",
      body: "A fresh setup no longer spams connection-refused errors at the default 8989/7878 ports while your real Sonarr/Radarr work. Existing setups self-heal automatically on this upgrade, no action needed.",
      icon: faServer,
      cta: { label: "Open Connections", to: "/settings/connections" },
    },
    {
      title: "Reverse-proxy subpath fixed",
      body: "Running Bazarr+ behind a reverse proxy on a subpath (base_url, e.g. /bazarr) no longer shows a blank page. Static assets now load correctly under the configured prefix.",
      icon: faTowerBroadcast,
    },
    {
      title: "Dependency & security maintenance",
      body: "A round of dependency and security updates across the stack (dynaconf, pillow, apscheduler, numpy, alembic, cloudscraper and more), with no known vulnerabilities outstanding.",
      icon: faShieldHalved,
    },
  ],
  "2.5.0": [
    {
      title: "Multiple Sonarr/Radarr instances",
      body: "Connect any number of Sonarr and Radarr servers to one Bazarr+. Every search, download, sync and webhook stays scoped to the server that owns each show or movie.",
      icon: faServer,
      cta: { label: "Open Connections", to: "/settings/connections" },
    },
    {
      title: "Per-instance subtitle settings",
      body: "Override subzero mods, post-processing, audio sync and keep-lyrics per instance. Bazarr+ resolves the right settings against the media's owning server.",
      icon: faSliders,
      cta: { label: "Open Connections", to: "/settings/connections" },
    },
    {
      title: "Archive uploads & drag-and-drop",
      body: "Drop a .zip, .rar or .7z of subtitles straight into the upload modal, or drag files anywhere onto a show or movie page.",
      icon: faFileZipper,
    },
    {
      title: "Guided first-run setup",
      body: "Fresh installs get a step-by-step wizard: connect Sonarr and Radarr, add Plex or Jellyfin, pick languages, and install subtitle providers, with the provider restart handled and resumed for you. It is skippable and never appears once you are set up.",
      icon: faWandMagicSparkles,
    },
    {
      title: "Hardened and polished",
      body: "SSRF and path-traversal fixes (local/LAN use is unaffected) plus 30+ frontend bug fixes, including a Subtitle Editor crash on plain-HTTP setups.",
      icon: faShieldHalved,
    },
  ],
  "2.4.0": [
    {
      title: "Distribution Hub",
      body: "Serve subtitles through a multi-tenant API with named keys, tiers, and per-key usage metering.",
      icon: faTowerBroadcast,
      cta: { label: "Open Distribution Hub", to: "/distribution-hub" },
    },
    {
      title: "Provider Hub auto-install",
      body: "Opt in to automatically replace built-in providers with their Provider Hub catalog versions at startup. Off by default; manual install from the Marketplace always works.",
      icon: faStore,
      cta: { label: "Open General settings", to: "/settings/general" },
    },
    {
      title: "Combined subtitles",
      body: "Merge subtitles from multiple languages into a single track for side-by-side viewing.",
      icon: faLayerGroup,
      cta: { label: "Open Subtitles settings", to: "/settings/subtitles" },
    },
    {
      title: "Smarter subtitle matching",
      body: "When a release name can't be parsed, Bazarr now falls back to the on-disk filename instead of giving up, so more searches succeed.",
      icon: faWandMagicSparkles,
    },
  ],
};

export function getWhatsNewSlides(version: string): WhatsNewSlide[] {
  return whatsNew[version] ?? [];
}
