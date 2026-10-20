import type { SubtitleSource } from "@/apis/raw/subtitles";

export function appendArrInstanceParam(url: string, arrInstanceId?: number) {
  if (arrInstanceId === undefined) {
    return url;
  }

  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}arr_instance_id=${encodeURIComponent(String(arrInstanceId))}`;
}

// The API key goes in the X-API-KEY header, never in the URL: a URL ends up
// in reverse proxy access logs.
export function buildEditorSubtitlesUrl(
  baseUrl: string,
  mediaType: string,
  mediaId: string,
  arrInstanceId?: number,
) {
  return appendArrInstanceParam(
    `${baseUrl}/api/editor/subtitles?mediaType=${encodeURIComponent(mediaType)}&mediaId=${encodeURIComponent(mediaId)}`,
    arrInstanceId,
  );
}

export function buildEditorAutosaveKey(
  mediaType?: string,
  mediaId?: string,
  language?: string,
  arrInstanceId?: number,
  source?: SubtitleSource,
  codec?: string,
) {
  if (!mediaType || !mediaId || !language) {
    return null;
  }

  const key =
    arrInstanceId === undefined
      ? `bazarr-editor-${mediaType}-${mediaId}-${language}`
      : `bazarr-editor-${mediaType}-${mediaId}-${arrInstanceId}-${language}`;
  // A draft of the embedded track is not a draft of the file that shares its
  // language key, and two text tracks of one key are two drafts: the source
  // and the row's codec both name the draft.
  if (!source) {
    return key;
  }
  return codec ? `${key}-${source}-${codec}` : `${key}-${source}`;
}

/** "embedded" when the editor was opened on the track inside the video. */
export function readEditorSource(
  searchParams: URLSearchParams,
): SubtitleSource | undefined {
  return searchParams.get("source") === "embedded" ? "embedded" : undefined;
}

/**
 * The codec the embedded row showed, when the URL names one. Two text tracks
 * with one language key and different codecs are two rows, so the read needs
 * the codec to pick the track the user clicked.
 */
export function readEditorCodec(
  searchParams: URLSearchParams,
): string | undefined {
  if (readEditorSource(searchParams) === undefined) {
    return undefined;
  }
  return searchParams.get("codec") || undefined;
}

/**
 * The editor's query string without `source` and `codec`: the two name the
 * track, and every navigation inside the editor (Save As, language switch,
 * the step after Save) lands on files.
 */
export function editorSearchWithoutSource(
  searchParams: URLSearchParams,
): string {
  const next = new URLSearchParams(searchParams);
  next.delete("source");
  next.delete("codec");
  return next.toString();
}

/**
 * The create route's fields for a language key: "en:hi" is en with HI. The
 * route refuses HI and forced together, and a download of a track that is
 * both is named forced, so a key with both saves as forced.
 */
export function createFieldsForLanguageKey(key: string): {
  language: string;
  hi: boolean;
  forced: boolean;
} {
  const [language, ...modifiers] = key.split(":");
  const lower = modifiers.map((modifier) => modifier.toLowerCase());
  const forced = lower.includes("forced");
  return { language, hi: !forced && lower.includes("hi"), forced };
}

/** Where View and Edit open a subtitle row. */
export function subtitleEditorUrl(
  action: "preview" | "edit",
  mediaType: "movie" | "episode" | "sports",
  mediaId: number,
  language: string,
  options: {
    arrInstanceId?: number | null;
    source?: SubtitleSource;
    codec?: string | null;
  } = {},
) {
  const path = `/subtitles/${action}/${mediaType}/${mediaId}/${encodeURIComponent(language)}`;
  const params = new URLSearchParams();
  if (options.arrInstanceId != null) {
    params.set("arr_instance_id", String(options.arrInstanceId));
  }
  if (options.source) {
    params.set("source", options.source);
    // The row's codec only names a track, never a file.
    if (options.codec) {
      params.set("codec", options.codec);
    }
  }
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

export interface EditorBreadcrumb {
  listPath: string;
  listLabel: string;
  detailPath?: string;
}

// The editor opens on three kinds of media now. "not episode means movie" sent
// every sports event to /movies/<leagueId>, a page that either does not exist
// or belongs to an unrelated film.
export function editorBreadcrumb(
  mediaType: string | undefined,
  mediaId: number | undefined,
  arrInstanceId?: number,
): EditorBreadcrumb {
  const scope = arrInstanceId === undefined ? "" : `?instance=${arrInstanceId}`;
  if (mediaType === "sports") {
    return {
      listPath: "/sports",
      listLabel: "Sports",
      detailPath: mediaId ? `/sports/${mediaId}${scope}` : undefined,
    };
  }
  if (mediaType === "episode" || mediaType === "series") {
    return {
      listPath: "/series",
      listLabel: "Series",
      detailPath: mediaId ? `/series/${mediaId}` : undefined,
    };
  }
  return {
    listPath: "/movies",
    listLabel: "Movies",
    detailPath: mediaId ? `/movies/${mediaId}` : undefined,
  };
}
