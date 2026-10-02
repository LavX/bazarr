import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it } from "vitest";
import { QueryKeys } from "@/apis/queries/keys";
import { settleSetupComplete } from "./setupCompleteCache";

const queryKey = [QueryKeys.System, QueryKeys.Settings];

function settings(complete: boolean): Settings {
  // eslint-disable-next-line camelcase
  return { general: { setup_complete: complete } } as unknown as Settings;
}

describe("settleSetupComplete", () => {
  let client: QueryClient;

  afterEach(() => {
    client.clear();
  });

  it("keeps the saved value when an older settings read lands after it", async () => {
    // Nothing observes the settings here. In the app the theme loader always
    // does, and only that made invalidateQueries cancel a read already on its
    // way. Without an observer the invalidation just marks the query stale, so
    // a read that left before the save could still answer with the old value
    // after the helper returned, and the Redirector would read it.
    client = new QueryClient({
      defaultOptions: { queries: { gcTime: Infinity, retry: false } },
    });
    client.setQueryData(queryKey, settings(false));

    let answer: ((value: Settings) => void) | undefined;
    const olderRead = client
      .fetchQuery({
        queryKey,
        queryFn: () =>
          new Promise<Settings>((resolve) => {
            answer = resolve;
          }),
      })
      .catch(() => undefined);
    expect(answer).toBeDefined();

    await settleSetupComplete(client, true);
    answer?.(settings(false));
    await olderRead;

    expect(
      client.getQueryData<Settings>(queryKey)?.general.setup_complete,
    ).toBe(true);
  });
});
