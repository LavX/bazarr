import type { MetadataSource } from "@/types/discover";

export function discoverTitlePath(
  source: MetadataSource,
  kind: "movie" | "show",
  id: number | string,
  season: number | null = null,
  episode: number | null = null,
) {
  const params = new URLSearchParams({ [kind]: String(id) });
  if (source !== "tmdb") params.set("source", source);
  if (kind === "show" && season !== null) {
    params.set("season", String(season));
    if (episode !== null) params.set("episode", String(episode));
  }
  return `/discover?${params}`;
}
