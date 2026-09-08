/* eslint-disable camelcase */

import type { ArrInstance } from "@/apis/raw/arrInstances";

export function makeInstance(
  overrides: Partial<ArrInstance> = {},
): ArrInstance {
  return {
    id: 42,
    kind: "sonarr",
    stable_key: "test-stable-key",
    name: "Main Sonarr",
    display_name: "Main Sonarr",
    enabled: true,
    is_default: false,
    ip: "192.168.1.10",
    port: 8989,
    base_url: "",
    ssl: false,
    verify_ssl: false,
    http_timeout: 30,
    api_key_set: true,
    subtitle_settings: {},
    media_defaults: {},
    ...overrides,
  };
}

export const sportarr = makeInstance({
  id: 42,
  kind: "sportarr",
  stable_key: "sportarr-main",
  name: "Main Sportarr",
  display_name: "Main Sportarr",
  port: 1867,
  is_default: true,
  media_defaults: { default_enabled: true, default_profile: 3 },
});

export const sportarrSibling = makeInstance({
  id: 43,
  kind: "sportarr",
  stable_key: "sportarr-archive",
  name: "Sportarr Archive",
  display_name: "Sportarr Archive",
  ip: "192.168.1.11",
  port: 1867,
});

export const sonarrDefault = makeInstance({
  id: 1,
  stable_key: "sonarr-main",
  ip: "192.168.1.20",
  is_default: true,
});

export const radarrDefault = makeInstance({
  id: 2,
  kind: "radarr",
  stable_key: "radarr-main",
  name: "Main Radarr",
  display_name: "Main Radarr",
  ip: "192.168.1.30",
  port: 7878,
  is_default: true,
});

export const sportsProfile: Language.Profile = {
  profileId: 3,
  name: "English Sports",
  cutoff: null,
  items: [
    {
      id: 0,
      language: "en",
      forced: "False",
      hi: "False",
      audio_exclude: "False",
      audio_only_include: "False",
      translate_from: null,
    },
  ],
  mustContain: [],
  mustNotContain: [],
  originalFormat: false,
  tag: "",
  combine: null,
};
