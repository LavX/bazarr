import type { QueryClient } from "@tanstack/react-query";
import { QueryKeys } from "./keys";

export function sourceSubtitleVariant(
  language: string,
  hi = false,
  forced = false,
) {
  if (forced) return `${language}:forced`;
  if (hi) return `${language}:hi`;
  return language;
}

export async function expireSourceSubtitleDocument(
  client: QueryClient,
  itemId: number,
  language: string,
) {
  const filters = {
    queryKey: [QueryKeys.Subtitles, "content", "source", itemId, language],
  };
  await client.cancelQueries(filters);
  await client.resetQueries(filters);
}
