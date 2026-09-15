import type { ArrKind } from "@/apis/raw/arrInstances";

export type LibraryMediaKind = "series" | "movies" | "sports";
export const LIBRARY_ROUTES: Record<ArrKind, `/${LibraryMediaKind}`> = {
  sonarr: "/series",
  radarr: "/movies",
  sportarr: "/sports",
};

export function libraryRouteForKind(kind: string) {
  return Object.hasOwn(LIBRARY_ROUTES, kind)
    ? LIBRARY_ROUTES[kind as ArrKind]
    : undefined;
}
