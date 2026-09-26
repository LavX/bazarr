import { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import type { MediaServerKind } from "@/apis/raw/mediaServers";
import { createDraft } from "@/pages/Setup/useOnboardingSelection";
import { act, renderHook, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import type { SubmitResult } from "./submit";
import { useMediaServerSubmit, validateDraft } from "./submit";

function draftOf(
  kind: MediaServerKind,
  overrides: Record<string, unknown> = {},
) {
  return {
    ...createDraft(kind),
    url: "http://10.0.0.9:8096",
    apiKey: "a-key",
    pathMappings: [{ local_path: "/tv", remote_path: "/media/tv" }],
    ...overrides,
  };
}

let creates: { kind: MediaServerKind; name: string }[] = [];
let settingsWrites: Record<string, unknown>[] = [];
let refuse: MediaServerKind | null = null;

function wrapper({ children }: PropsWithChildren) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, networkMode: "offlineFirst" },
      mutations: { retry: false, networkMode: "offlineFirst" },
    },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("useMediaServerSubmit", () => {
  beforeEach(() => {
    creates = [];
    settingsWrites = [];
    refuse = null;
    server.use(
      http.post("/api/system/media-server-instances", async ({ request }) => {
        const body = (await request.json()) as {
          kind: MediaServerKind;
          name: string;
        };
        creates.push(body);
        if (refuse === body.kind) {
          return HttpResponse.json({ message: "nope" }, { status: 400 });
        }
        return HttpResponse.json({
          id: `row-${creates.length}`,
          kind: body.kind,
          name: body.name,
          enabled: true,
          url: "http://10.0.0.9:8096",
          verify_ssl: true,
          api_key_set: true,
          path_mappings: [],
          refresh_movies: true,
          refresh_episodes: true,
          options: {},
        });
      }),
      http.post("/api/system/settings", async ({ request }) => {
        const form = await request.formData();
        settingsWrites.push(Object.fromEntries(form.entries()));
        return new HttpResponse(null, { status: 204 });
      }),
    );
  });

  it("checks every draft before it writes any of them", async () => {
    const { result } = renderHook(() => useMediaServerSubmit(), { wrapper });
    const good = draftOf("jellyfin", { pathMappings: [] });
    const bad = draftOf("emby", { url: "not-a-url" });

    let outcome: SubmitResult | undefined;
    await act(async () => {
      outcome = await result.current.submit([good, bad]);
    });

    // A run never writes half a selection because the last row had a typo.
    expect(creates).toEqual([]);
    expect(outcome!.errors[bad.draftId].url).toMatch(/full http/i);
    expect(outcome!.errors[good.draftId]).toBeUndefined();
  });

  it("writes several kinds and flips their switches in one settings write", async () => {
    const { result } = renderHook(() => useMediaServerSubmit(), { wrapper });

    let outcome: SubmitResult | undefined;
    await act(async () => {
      outcome = await result.current.submit([
        draftOf("jellyfin", { pathMappings: [] }),
        draftOf("emby"),
      ]);
    });

    expect(outcome!.outcomes.every((entry) => entry.ok)).toBe(true);
    await waitFor(() => expect(settingsWrites).toHaveLength(1));
    expect(settingsWrites[0]).toEqual({
      "settings-general-use_jellyfin": "true",
      "settings-general-use_emby": "true",
    });
  });

  it("leaves the rows that landed alone when one is refused", async () => {
    refuse = "emby";
    const { result } = renderHook(() => useMediaServerSubmit(), { wrapper });
    const jellyfin = draftOf("jellyfin", { pathMappings: [] });
    const emby = draftOf("emby");

    let outcome: SubmitResult | undefined;
    await act(async () => {
      outcome = await result.current.submit([jellyfin, emby]);
    });

    const byDraft = Object.fromEntries(
      outcome!.outcomes.map((entry) => [entry.draftId, entry]),
    );
    expect(byDraft[jellyfin.draftId].ok).toBe(true);
    expect(byDraft[jellyfin.draftId].instanceId).toBe("row-1");
    expect(byDraft[emby.draftId].ok).toBe(false);
    expect(byDraft[emby.draftId].error).toBeTruthy();
    // Only the kind that landed gets its switch.
    expect(settingsWrites[0]).toEqual({
      "settings-general-use_jellyfin": "true",
    });
  });

  it("keeps Plex out of the settings write", async () => {
    // media_servers.plex_account sets use_plex itself on the OAuth callback,
    // so writing it here would be a second write of a switch already set.
    const { result } = renderHook(() => useMediaServerSubmit(), { wrapper });

    await act(async () => {
      await result.current.submit([
        draftOf("plex", { pathMappings: [] }),
        draftOf("silo"),
      ]);
    });

    expect(settingsWrites[0]).toEqual({ "settings-general-use_silo": "true" });
  });
});

describe("validateDraft", () => {
  it("attributes each message to its own field", () => {
    const errors = validateDraft({
      ...createDraft("emby"),
      name: "  ",
      url: "",
      apiKey: "",
    });

    expect(errors.name).toMatch(/name is required/i);
    expect(errors.url).toMatch(/full http/i);
    expect(errors.apiKey).toMatch(/api key is required/i);
    expect(errors.pathMappings).toMatch(/at least one path mapping/i);
  });

  it("asks a kind that resolves by path for its mapping, and nothing else for one that does not", () => {
    const jellyfin = validateDraft({
      ...createDraft("jellyfin"),
      url: "http://10.0.0.9:8096",
      apiKey: "k",
    });

    expect(jellyfin).toEqual({});
  });
});
