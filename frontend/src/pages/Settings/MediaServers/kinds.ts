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

export function kindName(kind: MediaServerKind) {
  return KIND_NAMES[kind];
}
