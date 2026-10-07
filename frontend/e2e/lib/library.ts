/**
 * Finding media on an instance that has a library. The specs that need a real
 * media file, such as the subtitle tools or an upload, use these to pick one,
 * and skip on an instance that has none: a fresh container has no Sonarr,
 * Radarr or Sportarr at all, and no run of this suite has a library today,
 * so these skip wherever the suite runs. A read-only spec can run against a
 * real install, which is the only run that has a library; the stateful ones
 * wait for a seeded-library run.
 */
import type { APIRequestContext } from "@playwright/test";
import { expect } from "@playwright/test";

/** A subtitle as the movies API reports it. */
export interface LibrarySubtitle {
  code2: string;
  name: string;
  hi: boolean;
  forced: boolean;
  path: string | null;
}

/**
 * A movie, with the fields the library specs read. The verified shape of
 * GET /api/movies: the body is { data: [...] }, and each row carries the
 * canonical local id, the upstream radarrId, the owning arr_instance_id and
 * the subtitles array.
 */
export interface LibraryMovie {
  id: number;
  radarrId: number;
  arr_instance_id: number | null;
  title: string;
  path: string;
  profileId: number | null;
  subtitles: LibrarySubtitle[];
}

/**
 * The enabled arr kinds of this instance, from the same answer the sidebar
 * reads. An instance with none has no library at all.
 */
export async function enabledArrKinds(
  api: APIRequestContext,
): Promise<string[]> {
  const response = await api.get("/api/system/arr-instances");
  expect(response.ok()).toBe(true);
  const instances = (await response.json()) as {
    kind: string;
    enabled: boolean;
  }[];
  return [...new Set(instances.filter((i) => i.enabled).map((i) => i.kind))];
}

/**
 * The instance's movies, as many as the API sends back. The filter is the
 * API's own id[] list parameter, keyed by the canonical local id.
 */
export async function movies(
  api: APIRequestContext,
  id?: number,
): Promise<LibraryMovie[]> {
  const response = await api.get("/api/movies", {
    params: id === undefined ? undefined : { "id[]": id },
  });
  expect(response.ok()).toBe(true);
  const { data } = (await response.json()) as { data: LibraryMovie[] };
  // The endpoint answers with the bare list; anything else is a contract
  // change this library must hear about, not a silent empty library.
  expect(Array.isArray(data)).toBe(true);
  return data;
}

/**
 * The first movie with an external subtitle file on disk, which is what the
 * tools menu, a translation and an upload all need, or null when there is
 * none on this instance.
 */
export async function movieWithSubtitle(
  api: APIRequestContext,
): Promise<LibraryMovie | null> {
  const all = await movies(api);
  return (
    all.find((movie) =>
      (movie.subtitles ?? []).some(
        (subtitle) => typeof subtitle.path === "string" && subtitle.path !== "",
      ),
    ) ?? null
  );
}
