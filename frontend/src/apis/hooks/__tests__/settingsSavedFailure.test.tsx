/**
 * A settings save answered with 503 and a refresh-failed code was written: the
 * configuration and its rows are on disk, and only what the backend does after
 * the write failed. Every view a written save can change is reloaded then, as
 * after a save that succeeded. A request that also changed the metadata settings
 * gets the metadata code, and it can carry exclusions or media server switches
 * just the same.
 */

import { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { AxiosError, AxiosHeaders } from "axios";
import { describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks/system";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";

vi.mock("@/apis/raw", () => ({
  default: {
    system: {
      updateSettings: vi.fn(),
    },
  },
}));

function savedButNotApplied(code: string) {
  const config = { headers: new AxiosHeaders() };
  return new AxiosError(
    "Service Unavailable",
    "ERR_BAD_RESPONSE",
    config,
    null,
    {
      status: 503,
      statusText: "Service Unavailable",
      headers: {},
      config,
      data: { code, message: "Saved, but applying it failed." },
    },
  );
}

describe.each(["discover_settings_refresh_failed", "settings_refresh_failed"])(
  "a save answered with %s",
  (code) => {
    it("reloads every view a written save can change", async () => {
      vi.mocked(api.system.updateSettings).mockRejectedValueOnce(
        savedButNotApplied(code),
      );
      const client = new QueryClient({
        defaultOptions: { mutations: { networkMode: "offlineFirst" } },
      });
      const spy = vi.spyOn(client, "invalidateQueries");
      const wrapper = ({ children }: PropsWithChildren) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      );

      const { result } = renderHook(() => useSettingsMutation(), { wrapper });
      result.current.mutate({
        "settings-discover-locale": "hu-HU",
        "settings-sonarr-excluded_tags": ["anime"],
      });
      await waitFor(() => expect(result.current.isError).toBe(true));

      const keys = spy.mock.calls.map(
        (call) => (call[0] as { queryKey?: unknown[] } | undefined)?.queryKey,
      );
      for (const key of [
        [QueryKeys.System],
        [QueryKeys.ProviderHub],
        [QueryKeys.Series],
        [QueryKeys.Episodes],
        [QueryKeys.Movies],
        [QueryKeys.Wanted],
        [QueryKeys.Sports],
        [QueryKeys.Badges],
        [QueryKeys.Plex, "libraries"],
      ]) {
        expect(keys).toContainEqual(key);
      }
    });
  },
);
