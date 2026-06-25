// Tab keys for the consolidated Connections page. Each old settings route
// (/settings/sonarr, /radarr, /plex, /jellyfin) redirects to
// /settings/connections#<key>, and the page selects the matching tab from the
// URL hash. Keep this order in sync with the <Tabs.List> in index.tsx.
export const CONNECTION_TABS = [
  "sonarr",
  "radarr",
  "plex",
  "jellyfin",
] as const;

export type ConnectionTab = (typeof CONNECTION_TABS)[number];

export const DEFAULT_CONNECTION_TAB: ConnectionTab = "sonarr";

export function isConnectionTab(value: string): value is ConnectionTab {
  return (CONNECTION_TABS as readonly string[]).includes(value);
}

// Accepts either "#plex" or "plex"; unknown/empty -> default tab.
export function parseTabFromHash(hash: string): ConnectionTab {
  const key = hash.replace(/^#/, "");
  return isConnectionTab(key) ? key : DEFAULT_CONNECTION_TAB;
}
