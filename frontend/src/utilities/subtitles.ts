import type { HideTrackResult } from "@/apis/raw/subtitles";

const syncEngineLabels: Record<string, string> = {
  "sync-ffsubsync": "FFsubsync",
  "sync-autosubsync": "Autosubsync",
  "sync-alass": "ALASS",
};

const syncEngineOrder = ["sync-ffsubsync", "sync-autosubsync", "sync-alass"];
const syncOutputFilenameEngines = ["ffsubsync", "autosubsync", "alass"];
const syncOutputLanguageModifiers = Object.keys(syncEngineLabels);

export type SubtitleSyncStatus = {
  synced: boolean;
  confirmed: boolean;
  editedAfterSync: boolean;
  lastModified: number;
  lastSyncTimestamp: string | null;
  jobStatus?: "pending" | "running" | null;
  jobId?: number | null;
};

export type SubtitleSyncStatusPresentation = {
  icon: "sync" | "question" | "running";
  label: string;
};

export function buildSubtitleLanguageKey(subtitle: Subtitle): string {
  if (subtitle.language) {
    return subtitle.language;
  }

  let key = subtitle.code2;
  if (subtitle.hi) key += ":hi";
  if (subtitle.forced) key += ":forced";
  if (subtitle.modifier) key += `:${subtitle.modifier}`;
  return key;
}

/**
 * How the Hide track confirmation and the hidden-tracks list name a track:
 * the language, then its variant, "English (HI)". A sports row carries only
 * the code, so the sports page looks the name up before it calls this.
 */
export function embeddedTrackLabel(
  subtitle: Pick<Subtitle, "code2" | "name" | "hi" | "forced">,
): string {
  const variant = [subtitle.hi && "HI", subtitle.forced && "forced"]
    .filter(Boolean)
    .join(", ");
  const name = subtitle.name || subtitle.code2;
  return variant ? `${name} (${variant})` : name;
}

/**
 * Every embedded track one Hide on `track` takes. Bazarr+ tells embedded
 * tracks apart by language, HI and forced only, so an SRT and a PGS English
 * track on one remux are hidden together.
 */
export function embeddedTracksSharingKey(
  track: Subtitle,
  listing: Subtitle[],
): Subtitle[] {
  const same = listing.filter(
    (subtitle) =>
      !subtitle.path &&
      subtitle.code2 === track.code2 &&
      subtitle.hi === track.hi &&
      subtitle.forced === track.forced,
  );
  return same.length > 0 ? same : [track];
}

/**
 * The codecs one hide recorded. A hide takes every track with the same
 * language, HI and forced, so the server stores their codecs in one field,
 * "pgs, subrip", and none at all for tracks indexed before codecs were kept.
 */
export function hiddenTrackCodecs(codec: string | null): string[] {
  return (codec ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter((value) => value.length > 0);
}

/**
 * What to tell the user after a hide that did not make the language wanted,
 * or null when it did. The wanted computation counts an HI track as good as
 * a normal one and counts subtitle files too, so hiding the plain English
 * track can leave English satisfied, and a language the profile does not ask
 * for is never wanted.
 */
export function hiddenTrackOutcome(result: HideTrackResult): string | null {
  if (result.wanted) {
    return null;
  }
  const language = embeddedTrackLabel(result.hidden);
  if (result.covered_by.length > 0) {
    const covering = result.covered_by
      .map(
        (subtitle) =>
          `${embeddedTrackLabel(subtitle)} (${
            subtitle.embedded ? "embedded track" : "subtitle file"
          })`,
      )
      .join(", ");
    return `${language} is still covered by ${covering}, so Bazarr+ will not search for it.`;
  }
  return `${language} is not wanted for this title under its languages profile, so Bazarr+ will not search for it.`;
}

export function buildComparableSubtitleVariantKey(subtitle: Subtitle): string {
  const [base, ...modifiers] = buildSubtitleLanguageKey(subtitle).split(":");
  const variants = modifiers
    .map((modifier) => modifier.toLowerCase())
    .filter((modifier) => !syncOutputLanguageModifiers.includes(modifier))
    .sort();

  return [base, ...variants].join(":");
}

export function isCompatibleSyncOutputSubtitle(
  source: Subtitle,
  output: Subtitle,
): boolean {
  return (
    isSyncOutputSubtitle(output) &&
    buildComparableSubtitleVariantKey(output) ===
      buildComparableSubtitleVariantKey(source)
  );
}

export function isSyncOutputSubtitle(subtitle: Subtitle): boolean {
  if (isSyncOutputLanguageKey(subtitle.language)) {
    return true;
  }

  if (subtitle.modifier?.startsWith("sync-") === true) {
    return true;
  }

  const path = subtitle.path?.toLowerCase() ?? "";
  return syncOutputFilenameEngines.some((engine) =>
    hasFinalEngineFilenameSegment(path, engine),
  );
}

export function isSyncOutputLanguageKey(
  language: string | null | undefined,
): boolean {
  const modifiers = language
    ?.split(":")
    .slice(1)
    .map((item) => item.toLowerCase());
  return (
    modifiers?.some((modifier) =>
      syncOutputLanguageModifiers.includes(modifier),
    ) ?? false
  );
}

export function isCombinedOutputSubtitle(subtitle: Subtitle): boolean {
  if (isCombinedOutputLanguageKey(subtitle.language)) {
    return true;
  }
  return subtitle.modifier?.startsWith("combined-") === true;
}

export function isCombinedOutputLanguageKey(
  language: string | null | undefined,
): boolean {
  const modifiers = language
    ?.split(":")
    .slice(1)
    .map((item) => item.toLowerCase());
  return modifiers?.some((m) => m.startsWith("combined-")) ?? false;
}

export function getCombinedSecondaries(subtitle: Subtitle): string[] | null {
  const language = buildSubtitleLanguageKey(subtitle);
  const modifier = language
    .split(":")
    .slice(1)
    .find((m) => m.toLowerCase().startsWith("combined-"));
  if (!modifier) {
    return null;
  }
  return modifier.slice("combined-".length).split("-");
}

export function getCombinedLabel(subtitle: Subtitle): string {
  const secondaries = getCombinedSecondaries(subtitle);
  if (!secondaries) {
    return "";
  }
  return [subtitle.code2, ...secondaries]
    .map((c) => c.toUpperCase())
    .join(" + ");
}

export function getCombinedFormat(subtitle: Subtitle): "srt" | "ass" {
  return subtitle.path?.toLowerCase().endsWith(".ass") ? "ass" : "srt";
}

// Build the combine request that reproduces THIS combined artifact (its own
// primary + secondaries + format), so a Rebuild targets the selected file
// rather than falling back to whatever the profile rule currently is.
export function combineRequestForSubtitle(subtitle: Subtitle): {
  languages: string[];
  format: "srt" | "ass";
} | null {
  const secondaries = getCombinedSecondaries(subtitle);
  if (!secondaries) {
    return null;
  }
  return {
    languages: [subtitle.code2, ...secondaries],
    format: getCombinedFormat(subtitle),
  };
}

function hasFinalEngineFilenameSegment(path: string, engine: string): boolean {
  const filename = path.split(/[\\/]/).pop() ?? "";
  const marker = `.${engine}.`;
  const markerIndex = filename.lastIndexOf(marker);

  if (markerIndex <= 0) {
    return false;
  }

  const extension = filename.slice(markerIndex + marker.length);
  return extension.length > 0 && !extension.includes(".");
}

export function canSynchronizeSubtitle(subtitle: Subtitle): boolean {
  // Combined-output files are valid SRTs and can be synced like any other.
  // Only sync-engine outputs are non-syncable (they already are the sync result).
  return !isSyncOutputSubtitle(subtitle);
}

export function getSyncEngineLabel(modifier: string | null | undefined) {
  if (!modifier) {
    return "Original";
  }
  return syncEngineLabels[modifier] ?? modifier.replace(/^sync-/, "");
}

export function sortSyncOutputSubtitles(subtitles: Subtitle[]) {
  return [...subtitles].sort((left, right) => {
    const leftIndex = syncEngineOrder.indexOf(left.modifier ?? "");
    const rightIndex = syncEngineOrder.indexOf(right.modifier ?? "");
    return (
      (leftIndex === -1 ? 99 : leftIndex) -
      (rightIndex === -1 ? 99 : rightIndex)
    );
  });
}

export function getSubtitleSyncStatusPresentation(
  status: SubtitleSyncStatus | null | undefined,
): SubtitleSyncStatusPresentation | null {
  if (status?.jobStatus === "running") {
    return {
      icon: "running",
      label: "Sync running",
    };
  }

  if (status?.jobStatus === "pending") {
    return {
      icon: "running",
      label: "Sync queued",
    };
  }

  if (!status?.synced) {
    return null;
  }

  if (!status?.confirmed) {
    return {
      icon: "question",
      label: "Sync unconfirmed",
    };
  }

  return {
    icon: "sync",
    label: "Sync",
  };
}

// Mirrors bitmap_codecs in extract_embedded_subtitle()
// (bazarr/subtitles/tools/translate/batch.py). The indexer stores knowit's
// format lowercased, so pgs, vobsub and dvbsub are the values that occur; the
// other three keep the two lists identical.
export const BITMAP_SUBTITLE_CODECS = [
  "pgs",
  "vobsub",
  "dvd_subtitle",
  "dvbsub",
  "hdmv_pgs_subtitle",
  "dvd",
] as const;

// Mirrors OCR_CODECS in bazarr/subtitles/tools/embedded_streams.py: the
// image-based codecs OCR to text reads. DVB is left out on purpose.
export const OCR_SUBTITLE_CODECS = [
  "pgs",
  "hdmv_pgs_subtitle",
  "vobsub",
  "dvd_subtitle",
  "dvd",
] as const;

export function isOcrCodec(codec: string | null | undefined): boolean {
  if (!codec) {
    return false;
  }
  return (OCR_SUBTITLE_CODECS as readonly string[]).includes(
    codec.toLowerCase(),
  );
}

const subtitleCodecLabels: Record<string, string> = {
  pgs: "PGS",
  vobsub: "VobSub",
  dvbsub: "DVB",
  subrip: "SRT",
  ass: "ASS",
  ssa: "SSA",
  "mov text": "MOV Text",
};

// An unknown codec is text: rows indexed before the codec was stored have
// none, and they must keep behaving as they did until the next index.
export function isBitmapCodec(codec: string | null | undefined): boolean {
  if (!codec) {
    return false;
  }
  return (BITMAP_SUBTITLE_CODECS as readonly string[]).includes(
    codec.toLowerCase(),
  );
}

export function subtitleCodecLabel(
  codec: string | null | undefined,
): string | null {
  if (!codec) {
    return null;
  }
  const key = codec.toLowerCase();
  return subtitleCodecLabels[key] ?? key.toUpperCase();
}

export function describeEmbeddedTrack(
  prefix: string,
  codec: string | null | undefined,
): string {
  const label = subtitleCodecLabel(codec);
  if (!label) {
    return prefix;
  }
  return `${prefix} (${label}${isBitmapCodec(codec) ? ", image-based" : ""})`;
}
