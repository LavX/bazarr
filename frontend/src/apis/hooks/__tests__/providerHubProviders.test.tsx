/* eslint-disable camelcase -- backend payload fixtures use server field names. */
import { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useProviderHubProviders } from "@/apis/hooks/providerHub";
import { useSettingsMutation } from "@/apis/hooks/system";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import type {
  ProviderHubInstallation,
  ProviderHubRuntimeStatus,
} from "@/apis/raw/providerHub";

vi.mock("@/apis/raw", () => ({
  default: {
    providerHub: { providers: vi.fn() },
    system: { updateSettings: vi.fn() },
  },
}));
vi.mock("@mantine/notifications", () => ({ showNotification: vi.fn() }));

const providers = vi.mocked(api.providerHub.providers);
const updateSettings = vi.mocked(api.system.updateSettings);
let client: QueryClient;

function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function subdlProvider(
  runtimeStatus?: ProviderHubRuntimeStatus,
): ProviderHubInstallation {
  return {
    provider_id: "subdl",
    name: "SubDL",
    state: "active",
    ...(runtimeStatus ? { runtime_status: runtimeStatus } : {}),
  };
}

const availableQuota: ProviderHubRuntimeStatus = {
  entitled: true,
  exhausted: false,
  remaining: 8,
  limit: 10,
  reset_at: "2026-10-01T00:00:00Z",
  reported_at: "2026-09-24T10:00:00Z",
};

const exhaustedQuota: ProviderHubRuntimeStatus = {
  ...availableQuota,
  exhausted: true,
  remaining: 0,
  reported_at: "2026-09-24T10:10:00Z",
};

function discoverSettingsRefreshError() {
  return Object.assign(new Error("Settings saved, refresh failed"), {
    isAxiosError: true,
    response: {
      status: 503,
      data: { code: "discover_settings_refresh_failed" },
    },
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 60_000 },
    },
  });
});

afterEach(() => {
  client.clear();
  vi.useRealTimers();
});

describe("Provider Hub providers runtime status", () => {
  it("refetches cached providers on remount inside the stale window", async () => {
    providers
      .mockResolvedValueOnce([subdlProvider()])
      .mockResolvedValueOnce([subdlProvider(availableQuota)]);

    let utils = renderHook(() => useProviderHubProviders(), {
      wrapper,
    });
    await waitFor(() => expect(utils.result.current.data).toHaveLength(1));
    expect(utils.result.current.data?.[0]).not.toHaveProperty("runtime_status");
    utils.unmount();

    utils = renderHook(() => useProviderHubProviders(), {
      wrapper,
    });
    await waitFor(() =>
      expect(utils.result.current.data?.[0]?.runtime_status?.remaining).toBe(8),
    );

    expect(providers).toHaveBeenCalledTimes(2);
  });

  it("polls mounted providers when quota changes and when runtime status clears", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    providers
      .mockResolvedValueOnce([subdlProvider(availableQuota)])
      .mockResolvedValueOnce([subdlProvider(exhaustedQuota)])
      .mockResolvedValueOnce([subdlProvider()]);

    const { result } = renderHook(() => useProviderHubProviders(), {
      wrapper,
    });
    await waitFor(() =>
      expect(result.current.data?.[0]?.runtime_status).toEqual(availableQuota),
    );
    await act(async () => {
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(10_001);
    });
    expect(result.current.data?.[0]?.runtime_status).toEqual(exhaustedQuota);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_001);
    });
    expect(result.current.data?.[0]).not.toHaveProperty("runtime_status");
    expect(providers).toHaveBeenCalledTimes(3);
  });

  it("refetches providers immediately after disabling translation", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    providers
      .mockResolvedValueOnce([subdlProvider(availableQuota)])
      .mockResolvedValueOnce([subdlProvider()]);
    updateSettings.mockResolvedValueOnce(undefined);

    const { result: providerResult } = renderHook(
      () => useProviderHubProviders(),
      {
        wrapper,
      },
    );
    await waitFor(() =>
      expect(providerResult.current.data?.[0]?.runtime_status).toEqual(
        availableQuota,
      ),
    );
    const { result: settingsResult } = renderHook(() => useSettingsMutation(), {
      wrapper,
    });

    await act(async () => {
      await settingsResult.current.mutateAsync({
        "settings-subdl-ai_translate": false,
      });
    });
    await waitFor(
      () =>
        expect(providerResult.current.data?.[0]).not.toHaveProperty(
          "runtime_status",
        ),
      { timeout: 1_000 },
    );

    expect(updateSettings).toHaveBeenCalledWith({
      "settings-subdl-ai_translate": false,
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: [QueryKeys.ProviderHub],
    });
    expect(providers).toHaveBeenCalledTimes(2);
  });

  it("refreshes providers after settings save with a metadata refresh failure", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const error = discoverSettingsRefreshError();
    providers
      .mockResolvedValueOnce([subdlProvider(availableQuota)])
      .mockResolvedValueOnce([subdlProvider()]);
    updateSettings.mockRejectedValueOnce(error);

    const { result: providerResult } = renderHook(
      () => useProviderHubProviders(),
      { wrapper },
    );
    await waitFor(() =>
      expect(providerResult.current.data?.[0]?.runtime_status).toEqual(
        availableQuota,
      ),
    );
    const { result: settingsResult } = renderHook(() => useSettingsMutation(), {
      wrapper,
    });

    let mutationError: unknown;
    await act(async () => {
      try {
        await settingsResult.current.mutateAsync({
          "settings-subdl-ai_translate": false,
        });
      } catch (caught) {
        mutationError = caught;
      }
    });

    expect(mutationError).toBe(error);
    await waitFor(
      () =>
        expect(providerResult.current.data?.[0]).not.toHaveProperty(
          "runtime_status",
        ),
      { timeout: 1_000 },
    );
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: [QueryKeys.ProviderHub],
    });
    expect(providers).toHaveBeenCalledTimes(2);
  });

  it("does not refresh providers after an ordinary settings save failure", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const error = new Error("Settings save failed");
    providers.mockResolvedValueOnce([subdlProvider(availableQuota)]);
    updateSettings.mockRejectedValueOnce(error);

    const { result: providerResult } = renderHook(
      () => useProviderHubProviders(),
      { wrapper },
    );
    await waitFor(() =>
      expect(providerResult.current.data?.[0]?.runtime_status).toEqual(
        availableQuota,
      ),
    );
    const { result: settingsResult } = renderHook(() => useSettingsMutation(), {
      wrapper,
    });

    let mutationError: unknown;
    await act(async () => {
      try {
        await settingsResult.current.mutateAsync({
          "settings-subdl-ai_translate": false,
        });
      } catch (caught) {
        mutationError = caught;
      }
    });

    expect(mutationError).toBe(error);
    expect(providerResult.current.data?.[0]?.runtime_status).toEqual(
      availableQuota,
    );
    expect(invalidate).not.toHaveBeenCalledWith({
      queryKey: [QueryKeys.ProviderHub],
    });
    expect(providers).toHaveBeenCalledTimes(1);
  });
});
