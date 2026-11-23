import type { QueryClient } from "@tanstack/react-query";
import { QueryKeys } from "./keys";

const contentKey = [QueryKeys.Subtitles, "content", "source"] as const;

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
    queryKey: [...contentKey, itemId, language],
  };
  await client.cancelQueries(filters);
  await client.resetQueries(filters);
}

export function invalidateSourceSubtitleDocuments(
  client: QueryClient,
  itemIds: readonly number[],
  language?: string,
) {
  const ids = new Set(
    itemIds.filter((id) => Number.isSafeInteger(id) && id > 0),
  );
  if (ids.size === 0) return;
  return client.invalidateQueries({
    queryKey: contentKey,
    predicate: ({ queryKey: [, , , id, variant] }) =>
      typeof id === "number" &&
      ids.has(id) &&
      (language === undefined || variant === language),
  });
}
