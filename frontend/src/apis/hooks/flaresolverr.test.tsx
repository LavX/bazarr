/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { act, renderHook, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { useFlareSolverrDetect, useFlareSolverrStatus } from "./providers";
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

  it("keeps a Detect answer when an older status read finishes after it", async () => {
    let releaseOldRead: () => void = () => undefined;
    const oldRead = new Promise<void>((resolve) => {
      releaseOldRead = resolve;
    });
    let reads = 0;
    server.use(
      http.get("/api/providers/flaresolverr", async () => {
        reads += 1;
        // Only the first read is slow; it was asked before Detect answered.
        if (reads === 1) {
          await oldRead;
          return HttpResponse.json({
            data: {
              url: "",
              source: "none",
              detected: null,
              detecting: false,
              sibling_found: false,
            },
          });
        }
        return HttpResponse.json({ data: {} });
      }),
      http.post("/api/providers/flaresolverr", () =>
        HttpResponse.json({
          data: {
            url: "",
            source: "none",
            detected: "http://flaresolverr:8191",
            detecting: false,
            sibling_found: true,
          },
        }),
      ),
    );
    const { wrapper, client } = queryWrapper();
    const { result, unmount } = renderHook(
      () => ({
        status: useFlareSolverrStatus(),
        detect: useFlareSolverrDetect(),
      }),
      { wrapper },
    );
    try {
      await waitFor(() => expect(reads).toBe(1));
      await act(async () => {
        await result.current.detect.mutateAsync();
      });
      await waitFor(() =>
        expect(result.current.status.data?.detected).toBe(
          "http://flaresolverr:8191",
        ),
      );

      await act(async () => {
        releaseOldRead();
        await new Promise((resolve) => setTimeout(resolve, 50));
      });

      expect(result.current.status.data?.detected).toBe(
        "http://flaresolverr:8191",
      );
      expect(result.current.status.data?.sibling_found).toBe(true);
    } finally {
      releaseOldRead();
      unmount();
      client.clear();
    }
  });
});
