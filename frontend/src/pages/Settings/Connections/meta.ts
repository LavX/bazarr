import {
  faFilm,
  faPlay,
  IconDefinition,
} from "@fortawesome/free-solid-svg-icons";
import type {
  ArrInstance,
  ArrInstanceCreate,
  ArrKind,
  ArrSubtitleSettings,
} from "@/apis/raw/arrInstances";
import { Environment } from "@/utilities/env";

interface ArrKindMeta {
  label: string;
  media: string;
  icon: IconDefinition;
  defaultPort: number;
}

export const ARR_META: Record<ArrKind, ArrKindMeta> = {
  sonarr: {
    label: "Sonarr",
    media: "series",
    icon: faPlay,
    defaultPort: 8989,
  },
  radarr: {
    label: "Radarr",
    media: "movies",
    icon: faFilm,
    defaultPort: 7878,
  },
};

export function buildHostUrl(instance: ArrInstance) {
  const protocol = instance.ssl ? "https" : "http";
  const base =
    instance.base_url && instance.base_url !== "/" ? instance.base_url : "";
  return `${protocol}://${instance.ip || "127.0.0.1"}:${instance.port}${base}`;
}

export function normalizeBaseUrl(value: string) {
  const trimmed = value.trim().replace(/^\/+/, "").replace(/\/+$/, "");
  return trimmed ? `/${trimmed}` : "";
}

export function stripLeadingSlash(value: string) {
  return value.replace(/^\/+/, "");
}

// Returns the per-instance webhook URL to display and copy. Uses the full
// origin plus the configured Bazarr base URL so subpath deployments work.
export function buildWebhookUrl(
  instance: Pick<ArrInstance, "kind" | "stable_key">,
  origin = window.location.origin,
  baseUrl = Environment.baseUrl,
): string {
  return `${origin}${baseUrl}/api/webhooks/${instance.kind}/${instance.stable_key}`;
}

// Builds the body for an arr instance create request. Omits is_default when
// the switch is off so the backend can auto-promote the first enabled instance.
export function buildArrInstanceCreateBody(
  kind: ArrKind,
  fields: {
    name: string;
    ip: string;
    port: number;
    base_url: string;
    ssl: boolean;
    verify_ssl: boolean;
    http_timeout: number;
    enabled: boolean;
    isDefault: boolean;
    apiKey?: string;
    subtitleSettings?: ArrSubtitleSettings;
  },
): ArrInstanceCreate {
  const body: ArrInstanceCreate = {
    kind,
    name: fields.name,
    ip: fields.ip,
    port: fields.port,
    base_url: fields.base_url,
    ssl: fields.ssl,
    verify_ssl: fields.verify_ssl,
    http_timeout: fields.http_timeout,
    enabled: fields.enabled,
  };
  if (fields.isDefault) {
    body.is_default = true;
  }
  if (fields.apiKey) {
    body.api_key = fields.apiKey;
  }
  // Only sent when at least one override is set; an empty block is omitted so a
  // new instance starts out fully inheriting the global settings.
  if (
    fields.subtitleSettings &&
    Object.keys(fields.subtitleSettings).length > 0
  ) {
    body.subtitle_settings = fields.subtitleSettings;
  }
  return body;
}
