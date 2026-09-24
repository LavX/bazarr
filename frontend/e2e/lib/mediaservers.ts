/**
 * Media server destinations, created and removed through the same API the
 * Connections page uses (bazarr/api/system/media_server_instances.py), and the
 * wizard walk up to the media server steps.
 */
import type { APIRequestContext, Page } from "@playwright/test";
import { expect } from "@playwright/test";
import { chooseIntent, stepHeading } from "./wizard";

export type MediaServerKind = "emby" | "jellyfin" | "plex" | "silo";

export interface PathMapping {
  local_path: string;
  remote_path: string;
  library_id?: string;
}

/** One row as GET /api/system/media-server-instances returns it. */
export interface MediaServerInstance {
  id: string;
  kind: MediaServerKind;
  name: string;
  enabled: boolean;
  url: string;
  verify_ssl: boolean;
  api_key_set: boolean;
  path_mappings: PathMapping[];
  refresh_movies: boolean;
  refresh_episodes: boolean;
}

export interface MediaServerCreate {
  kind: MediaServerKind;
  name: string;
  url: string;
  api_key?: string;
  enabled?: boolean;
  path_mappings?: PathMapping[];
}

/**
 * A server nothing listens on. Inside the container the port is refused at
 * once, so a test or a status probe fails in milliseconds, not on a timeout.
 */
export const BOGUS_URL = "http://127.0.0.1:9";
export const BOGUS_KEY = "not-a-real-key";

const ROOT = "/api/system/media-server-instances";

export async function listMediaServers(
  api: APIRequestContext,
): Promise<MediaServerInstance[]> {
  const response = await api.get(ROOT);
  expect(response.status(), `GET ${ROOT}`).toBe(200);
  const body = (await response.json()) as { data: MediaServerInstance[] };
  return body.data;
}

export async function createMediaServer(
  api: APIRequestContext,
  body: MediaServerCreate,
): Promise<MediaServerInstance> {
  const response = await api.post(ROOT, { data: body });
  expect(response.status(), `POST ${ROOT} for ${body.kind}`).toBe(201);
  return (await response.json()) as MediaServerInstance;
}

export async function deleteMediaServer(
  api: APIRequestContext,
  id: string,
): Promise<void> {
  const response = await api.delete(`${ROOT}/${id}`);
  expect(response.status(), `DELETE ${ROOT}/${id}`).toBe(204);
}

export async function deleteAllMediaServers(
  api: APIRequestContext,
): Promise<void> {
  for (const instance of await listMediaServers(api)) {
    await deleteMediaServer(api, instance.id);
  }
}

/**
 * Turns on the per-kind master switches, as the wizard does after a save. An
 * instance only refreshes, and only shows on System > Status, with both its
 * own switch and its kind's switch on.
 */
export async function switchOnKinds(
  api: APIRequestContext,
  kinds: MediaServerKind[],
): Promise<void> {
  const multipart: Record<string, string> = {};
  for (const kind of kinds) multipart[`settings-general-use_${kind}`] = "true";
  const response = await api.post("/api/system/settings", { multipart });
  expect(response.ok(), "saving the media server switches").toBe(true);
}

/**
 * The What's New tour opens by itself on a browser's first visit to the app.
 * Closes it whenever it gets in the way, the way a reader would.
 */
export async function closeWhatsNewWhenShown(page: Page): Promise<void> {
  await page.addLocatorHandler(
    page.getByRole("dialog", { name: "What's New" }),
    async () => {
      await page.keyboard.press("Escape");
    },
  );
}

/**
 * Library intent, past the three arr steps without filling anything in, to
 * the media server picker.
 */
export async function openMediaServerPicker(page: Page): Promise<void> {
  await chooseIntent(page, "library");
  for (const arr of ["Sonarr", "Radarr", "Sportarr"]) {
    await expect(stepHeading(page, arr)).toBeVisible();
    await page.getByRole("button", { name: `Continue without ${arr}` }).click();
  }
  await expect(stepHeading(page, "Media servers")).toBeVisible();
}

/** Ticks Jellyfin and Emby, adds a second Jellyfin, and opens the first step. */
export async function setUpJellyfinTwiceAndEmby(page: Page): Promise<void> {
  await openMediaServerPicker(page);
  await page.getByRole("checkbox", { name: "Jellyfin", exact: true }).click();
  await page.getByRole("checkbox", { name: "Emby", exact: true }).click();
  await page.getByRole("button", { name: "Add another Jellyfin" }).click();
  await page.getByRole("button", { name: "Set up 3 servers" }).click();
}

/** The server a configure step is for, its level-3 title. */
export function serverHeading(page: Page) {
  return page.getByRole("heading", { level: 3 });
}
