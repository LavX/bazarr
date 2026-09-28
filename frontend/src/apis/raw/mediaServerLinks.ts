/* eslint-disable camelcase */

import BaseApi from "./base";
import { MEDIA_SERVER_KINDS, MediaServerKind } from "./mediaServers";

export type LinkMediaType = "movie" | "series" | "episode" | "sports";

/** A destination a link can open, and the name its row shows. */
export type MediaServerLinkTarget = {
  instance_id: string;
  kind: MediaServerKind;
  display_name: string;
};

export type MediaServerLinkAnswer = {
  instance_id: string;
  state: "resolved" | "not_found" | "unreachable";
  url: string | null;
};

const root = "/system/media-server-links";

function safeTargets(data: unknown): MediaServerLinkTarget[] {
  const rows = (data as { data?: unknown })?.data;
  if (
    !Array.isArray(rows) ||
    rows.some(
      (row) =>
        !row ||
        typeof row.instance_id !== "string" ||
        !MEDIA_SERVER_KINDS.includes(row.kind) ||
        typeof row.display_name !== "string",
    )
  ) {
    throw new Error("Invalid media server link response");
  }
  return rows.map((row) => ({
    instance_id: row.instance_id,
    kind: row.kind,
    display_name: row.display_name,
  }));
}

// Only an http(s) address becomes an href, whatever the server sent.
const isWebUrl = (value: unknown): value is string =>
  typeof value === "string" && /^https?:\/\//i.test(value);

class MediaServerLinksApi extends BaseApi {
  constructor() {
    super(root);
  }

  async destinations() {
    return safeTargets(await this.get("/destinations"));
  }

  async candidates(mediaType: LinkMediaType, id: number) {
    return safeTargets(
      await this.get("/candidates", { media_type: mediaType, id }),
    );
  }

  async resolve(
    instanceId: string,
    mediaType: LinkMediaType,
    id: number,
  ): Promise<MediaServerLinkAnswer> {
    const data = await this.get<MediaServerLinkAnswer>("/resolve", {
      instance_id: instanceId,
      media_type: mediaType,
      id,
    });
    if (data?.state === "resolved" && isWebUrl(data.url)) {
      return { instance_id: instanceId, state: "resolved", url: data.url };
    }
    return {
      instance_id: instanceId,
      state: data?.state === "not_found" ? "not_found" : "unreachable",
      url: null,
    };
  }
}

export default new MediaServerLinksApi();
