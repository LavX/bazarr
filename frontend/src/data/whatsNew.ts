import type { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import {
  faChartLine,
  faClockRotateLeft,
  faCompass,
  faDatabase,
  faDownload,
  faFileLines,
  faFileZipper,
  faFilm,
  faGaugeHigh,
  faLayerGroup,
  faListCheck,
  faMagnifyingGlass,
  faPaperPlane,
  faScaleBalanced,
  faServer,
  faShieldHalved,
  faSliders,
  faStore,
  faTag,
  faTowerBroadcast,
  faTrophy,
  faWandMagicSparkles,
} from "@fortawesome/free-solid-svg-icons";

export interface WhatsNewSlide {
  /** Short headline for the change. */
  title: string;
  /**
   * Plain-language prose describing it, two to five sentences. The modal wraps
   * and scrolls it, so the bodies from v2.6.0 on run to a short paragraph;
   * verified rendering at 400px and 1280px wide.
   */
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
export const latestWhatsNewVersion = "2.7.0";

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
  "2.7.0": [
    {
      title: "Discover is where Bazarr+ opens now",
      body: "Instead of a table, the first thing you see is your own library: what it holds, what it is working on, and what is still missing, grouped per library with a link into each queue. Needs attention collects throttled providers, disconnected library sync and unreachable root folders, and stays quiet when there is nothing to report. Below your library sit trending titles, digital releases and recent episodes.",
      icon: faCompass,
      cta: { label: "Open Discover", to: "/discover" },
    },
    {
      title: "Search subtitles for any title, owned or not",
      body: "Look up any film or show by title and search your providers for it, even with an empty library and no Sonarr or Radarr connected. Each provider reports its own progress as it runs, the result opens as a formatted preview under the row, and Download saves the file straight to the device you are reading on. Browsing and refreshing never contact a subtitle provider: only a search you asked for does. The download runs as a job and the file saves itself the moment it is ready. If you leave the page first, Save in the Jobs drawer keeps it for up to 30 minutes.",
      icon: faMagnifyingGlass,
      cta: { label: "Open Discover", to: "/discover" },
    },
    {
      title: "Discover shows subtitles while the search is still running",
      body: "A search across dozens of providers used to show nothing until the last one answered, although the progress bar was already counting results. Rows now appear as each provider finishes, in the order they arrive, and you can download one while the rest are still working: on a real install the first rows landed at 6 seconds on a search that finished at 21. Providers are reported honestly too, so one that never got to run is no longer recorded as a timeout, and a failing site now backs off the way it does for a library search instead of being hit again every minute.",
      icon: faMagnifyingGlass,
      cta: { label: "Open Discover", to: "/discover" },
    },
    {
      title: "Jobs tell you when they are done, and why they failed",
      body: "A manual download, upload or sync that failed used to be listed as completed, and some actions never showed in the Jobs button at all. Downloads, uploads, syncs, subtitle tools, scan disk, combine, Provider Hub installs, editor translations and waveforms now all run as jobs, and saving a language profile returns at once while a Recalculating missing subtitles job does the library pass. A failed job says why in its notification and in the Jobs drawer, with Retry when another attempt can help, and a finished one that has something for you, like a Discover download, offers it right there. Tables refresh when the job finishes rather than when it is queued.",
      icon: faListCheck,
    },
    {
      title: "No library? The app stops offering what it cannot open",
      body: "On an install with no Sonarr, Radarr or Sportarr, the sidebar listed Series, Movies and Sports anyway and every one of them was a dead end. Those entries now appear with the integration that owns them, per kind, so an install running only Radarr gets Movies and not Series. Discover's line about connecting a library became a notice you can close for good, and the heading that marked where your own library ends is gone on a page that has no library half to divide from.",
      icon: faCompass,
      cta: { label: "Open Discover", to: "/discover" },
    },
    {
      title: "The setup wizard can install the providers that need no account",
      body: "A fresh install used to show a long list of provider checkboxes with nothing to say which of them work without signing up for something. The Providers step now leads with one action that installs and enables every provider needing no account, no configuration and no helper service, and it names the count before you click. The set is read off the catalog manifests rather than kept in a list, so it stays right as the catalog changes, and anything it leaves out is still one tick away in the same list.",
      icon: faStore,
      cta: { label: "Open Subtitle Hub", to: "/subtitle-hub" },
    },
    {
      title: "The setup wizard takes every media server you run",
      body: "The wizard offered one media server kind behind a radio button, and switching kinds threw away what you had typed, so an install running Jellyfin and Plex, or two Embys, could not say so during setup. You now tick as many kinds as you run, add a second server of a kind, and get one screen per server. Pressing Continue on an untouched Sonarr step used to create an enabled instance with no address and no key and switch Sonarr on, which the Finish screen then reported as connected; an untouched step writes nothing at all now. Every step also fits a 1920 by 1080 screen with nothing hidden behind a scrollbar. Setup no longer has to be finished in one sitting either: Set up later asks before it ends onboarding, and Settings, General has a Run first-time setup control that reopens the wizard.",
      icon: faServer,
      cta: { label: "Open Connections", to: "/settings/connections" },
    },
    {
      title: "Request a title in Seerr from Discover",
      body: "If you run Overseerr, Jellyseerr or Seerr, a title page says what Seerr already knows and offers the matching action: request it, pick seasons, or nothing at all when it is already available or blocklisted. A series opens a season picker that keeps what Seerr holds, what you already own and what is left to request apart. Bazarr+ never approves anything itself, but requests are made as the Seerr owner, which Seerr approves immediately, and the page says so beside the action.",
      icon: faPaperPlane,
      cta: { label: "Open Connections", to: "/settings/connections" },
    },
    {
      title: "Sports are a media type, not a side door",
      body: "Sportarr recordings now go through the same workflows as series and movies: manual search and download, uploads, the subtitle editor, sync, translate, combine, Wanted, History, Excluded and global search. Every job, file read and provider callback stays with the Sportarr instance that owns the recording. Subtitle settings take a global default with per-instance overrides, and disabled instances drop out of scheduled work.",
      icon: faTrophy,
      cta: { label: "Open Sports", to: "/sports" },
    },
    {
      title: "Several Emby and Silo servers, not one of each",
      body: "Add as many Emby and Silo servers as you run, each with its own URL, encrypted key, TLS setting and path mappings, the way Sonarr and Radarr already worked. A new subtitle is refreshed on every server whose mappings cover that file, and one unreachable server no longer holds up the rest. Emby matches an item by provider id, then exact path, then title and year; Silo matches by path and falls back to a library scan. Both are new here, and both cover movies, episodes and sports recordings. The Emby and Silo sections in Connections link out to a guide covering path mappings, the refresh states and what each server can match on.",
      icon: faServer,
      cta: { label: "Open Connections", to: "/settings/connections" },
    },
    {
      title: "Statistics that answer something",
      body: "System, Statistics plots downloads per day with the share that arrived without a manual search, downloads and mean match quality per provider with a blacklist rate beside them, the spread of match scores, and which languages you actually end up with. A provider high on downloads and high on blacklist rate is the one to turn off, which nothing surfaced before. Scores are normalised per media type first, so an episode out of 360 and a film out of 180 no longer average into a meaningless number.",
      icon: faChartLine,
      cta: { label: "Open Statistics", to: "/system/statistics" },
    },
    {
      title: "Announcements you can actually read",
      body: 'The announcements page was a four column table that gave the date 79px and the announcement 1614px, so "4 months ago" wrapped onto four lines while a whole paragraph sat unbroken beside it, and at 400px the Dismiss button was off the screen. It is a stack of cards in the same shape as the release notes now: a kind, a headline, the age on one line, and the body cut to four lines with a control that opens the rest. A link the feed left mid sentence is a link, and dismissing works exactly as before.',
      icon: faTowerBroadcast,
      cta: { label: "Open Announcements", to: "/system/announcements" },
    },
    {
      title: "A log worth attaching to a bug report",
      body: "Debug mode used to be the only control over the log and it turned on everything at once, including two loggers that write a line per event: a replay of one debug day produced 957,284 rows and 158MB. Those two and the scheduler now stop at WARNING in debug, so a real fault still shows and the flood does not. A normal install gains the provider lifecycle lines that explain why a search found nothing, roughly two per provider and one per candidate, with debug left off.",
      icon: faFileLines,
      cta: { label: "Open System Logs", to: "/system/logs" },
    },
    {
      title: "System Status shows what you actually run",
      body: "The Status page listed Sonarr and Radarr whether or not they were configured, so a fresh install read two rows with nothing beside them, and it never mentioned a media server at all. A row now appears only for an integration that is configured and has answered, and every Emby, Jellyfin, Silo and Plex destination gets one of its own with the version read from the server. An unreachable server says so instead of rendering blank, a Silo says it is connected without inventing a version it does not publish, and the page keeps checking while one is still being probed.",
      icon: faGaugeHigh,
      cta: { label: "Open System Status", to: "/system/status" },
    },
    {
      title: "SmartFast routing: cheaper OpenRouter translations if you switch",
      body: "SmartFast asks OpenRouter for an endpoint that is cheap and still fast enough, rather than the fastest one at any price. Nothing changes on your install: your current routing is kept until you pick SmartFast yourself under Settings, AI Translator, Provider Routing. It is worth the click if you are on Fastest, which is where every install that predates the routing selector sits: the September benchmark runs kept landing it on endpoints priced around twice the cheapest endpoint serving the same model. SmartFast needs AI Subtitle Translator 2.0.0 or newer and an older service refuses it, so update the translator before you switch.",
      icon: faGaugeHigh,
      cta: { label: "Open Translator settings", to: "/settings/translator" },
    },
    {
      title: "Translations that stall, vanish or talk over each other",
      body: "Setting reasoning to Disabled now really disables it instead of leaving the model's own default running, which is what had jobs timing out on reasoning tokens, and a cleared setting counts as disabled too. Thanks to wouterrutgers for finding and fixing the first half of that. Progress also stays visible until the file is published rather than ending while the service is still finalising, and two translations running at once each keep their own progress, on their own job in the Jobs button.",
      icon: faWandMagicSparkles,
      cta: { label: "Open Translator settings", to: "/settings/translator" },
    },
    {
      title: "Sessions, cookies and the event stream are closed up",
      body: "A failed form login used to leave a cookie good enough to fetch the log file and the config backup, and that backup carries every credential in the install. That is closed. The legacy password upgrade no longer routes your plaintext password through the browser cookie, CORS stays off unless you turn it on, and the event socket refuses anyone who cannot prove who they are. Everyone is signed out once on this upgrade, and session lifetime, cookie security and the trusted proxy are read at startup, so changing them needs a restart.",
      icon: faShieldHalved,
      cta: { label: "Open General settings", to: "/settings/general" },
    },
    {
      title: "Combined subtitles keep their characters",
      body: "Combining subtitles preserves valid UTF-8 and BOM-marked Unicode instead of guessing an encoding, so accented and non-Latin text survives the merge. Encoding detection is now a fallback for legacy files only. A movie's translated badge also follows the file on disk, so replacing or deleting a subtitle clears a stale badge while a later sync keeps a current translation marked.",
      icon: faLayerGroup,
      cta: { label: "Open Subtitles settings", to: "/settings/subtitles" },
    },
    {
      title: "Editor playback works on your second Sonarr or Radarr",
      body: "Video playback in the subtitle editor failed for media on an explicitly selected instance: the playlist loaded and everything it pointed at came back 404. The selected instance is now carried through the initialization and media requests too.",
      icon: faFilm,
      cta: { label: "Open your series", to: "/series" },
    },
    {
      title: "A failed search or download tells you what went wrong",
      body: "A manual search or download that failed showed a fixed sentence pointing you at the file and the providers, while the real reason was already in the response and in the log. Episodes, movies and sports all threw it away, so a subtitle that arrived and could not be used and a provider that was throttled read exactly the same. The reason now reaches the dialog, and the old sentence is kept only for a failure that carried none.",
      icon: faMagnifyingGlass,
      cta: { label: "Open your series", to: "/series" },
    },
    {
      title: "The tab says which build you are running",
      body: "Page titles used to stop at the instance name. They now end with the running version, so a tab reads Series - Bazarr+ v2.7.0, and Discover sets a title at all. A custom instance name is still the base, so it follows whatever you named this install.",
      icon: faTag,
      cta: { label: "Open System Status", to: "/system/status" },
    },
    {
      title: "Translated upgrades are their own switch, and off",
      body: "Translated subtitles shared the upgrade toggle with manual downloads, and that toggle was on by default, so the upgrade job kept replacing a translation with a provider listing every cycle even when the score did not improve. Settings, Subtitles now has one switch for manually downloaded or uploaded subtitles and another for translated ones. Both are off on a new install. On this upgrade your manual setting is kept and translated upgrades are off, so turn them on if you want provider subtitles to replace your translations.",
      icon: faSliders,
      cta: { label: "Open Subtitles settings", to: "/settings/subtitles" },
    },
    {
      title: "The same subtitle stops being downloaded every cycle",
      body: "With translated upgrades on, a translation could be upgraded forever: the same provider listing, the same score, every cycle, until Bazarr+ was stopped to halt the traffic. The upgrade wrote its result under the language the provider returned while the translation kept its hearing-impaired or forced variant, so nothing ever replaced the row the upgrade measured against, and deleting the translated file let the next scan translate it again. A translation is now replaced by a real subtitle once instead of once per cycle, and a genuinely better listing still upgrades.",
      icon: faClockRotateLeft,
      cta: { label: "Open Subtitles settings", to: "/settings/subtitles" },
    },
  ],
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
