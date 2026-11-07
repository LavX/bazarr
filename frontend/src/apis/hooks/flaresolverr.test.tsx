import { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { act, renderHook, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { useFlareSolverrStatus } from "./providers";
import { useSettingsMutation } from "./system";

function queryWrapper() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, networkMode: "offlineFirst" },
      mutations: { retry: false, networkMode: "offlineFirst" },
    },
  });
  return {
    client,
    wrapper: ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  };
}

describe("FlareSolverr status", () => {
  it("is read again once a settings save has gone through", async () => {
    let saved = false;
    server.use(
      http.get("/api/providers/flaresolverr", () =>
        HttpResponse.json({
          data: saved
            ? { url: "http://typed:8191", source: "setting", detected: null }
            : { url: "http://env:8191", source: "env", detected: null },
        }),
      ),
      http.post("/api/system/settings", () => {
        saved = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { wrapper, client } = queryWrapper();
    const { result, unmount } = renderHook(
      () => ({ status: useFlareSolverrStatus(), save: useSettingsMutation() }),
      { wrapper },
    );
    try {
      await waitFor(() =>
        expect(result.current.status.data?.source).toBe("env"),
      );
      await act(async () => {
        await result.current.save.mutateAsync({
          "settings-endpoints-flaresolverr_v1_url": "http://typed:8191",
        });
      });
      await waitFor(() =>
        expect(result.current.status.data?.source).toBe("setting"),
      );
    } finally {
      unmount();
      client.clear();
    }
  });
});
