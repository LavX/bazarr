import type { MediaServerKind } from "@/apis/raw/mediaServers";

export const KIND_NAMES: Record<MediaServerKind, string> = {
  emby: "Emby",
  jellyfin: "Jellyfin",
  plex: "Plex",
  silo: "Silo",
};

export const URL_PLACEHOLDERS: Record<MediaServerKind, string> = {
  emby: "http://192.168.1.100:8096",
  jellyfin: "http://192.168.1.100:8096",
  plex: "http://192.168.1.100:32400",
  silo: "https://silo.example",
};

// Plex authenticates with an X-Plex-Token; the field is the same shape and the
// same secret handling, so only the word the user reads changes.
export const CREDENTIAL_LABELS: Record<MediaServerKind, string> = {
  emby: "API Key",
  jellyfin: "API Key",
  plex: "Plex token",
  silo: "API Key",
};

// The guide's own anchors. Emby is the first half of that page and Silo has its
// own section; Jellyfin and Plex have none yet, so they land on the page, whose
// shared half is what they need: path mappings, the refresh states, and what
// each server can match on.
export const GUIDE_URL =
  "https://lavx.github.io/bazarr/guides/media-servers.html";
export const GUIDE_ANCHORS: Record<MediaServerKind, string> = {
  emby: "",
  jellyfin: "",
  plex: "",
  silo: "#silo",
};

export function kindName(kind: MediaServerKind) {
  return KIND_NAMES[kind];
}
