import {
  faFilm,
  faPlay,
  IconDefinition,
} from "@fortawesome/free-solid-svg-icons";
import type { ArrInstance, ArrKind } from "@/apis/raw/arrInstances";

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
