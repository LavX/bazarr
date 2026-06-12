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
