export function appendArrInstanceParam(url: string, arrInstanceId?: number) {
  if (arrInstanceId === undefined) {
    return url;
  }

  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}arr_instance_id=${encodeURIComponent(String(arrInstanceId))}`;
}

export function buildEditorSubtitlesUrl(
  baseUrl: string,
  mediaType: string,
  mediaId: string,
  apiKey: string,
  arrInstanceId?: number,
) {
  return appendArrInstanceParam(
    `${baseUrl}/api/editor/subtitles?mediaType=${encodeURIComponent(mediaType)}&mediaId=${encodeURIComponent(mediaId)}&apikey=${encodeURIComponent(apiKey)}`,
    arrInstanceId,
  );
}

export function buildEditorAutosaveKey(
  mediaType?: string,
  mediaId?: string,
  language?: string,
  arrInstanceId?: number,
) {
  if (!mediaType || !mediaId || !language) {
    return null;
  }

  return arrInstanceId === undefined
    ? `bazarr-editor-${mediaType}-${mediaId}-${language}`
    : `bazarr-editor-${mediaType}-${mediaId}-${arrInstanceId}-${language}`;
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
