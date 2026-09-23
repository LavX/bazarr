/**
 * Provider Hub lifecycle mutations follow the backend job they queue.
 *
 * The routes answer 202 with a job id; the mutation settles when the jobs
 * socket reports that job completed or failed in the [System, Jobs] cache, so
 * isPending covers the whole install and a failure carries the job's reason.
 */

import { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  useProviderHubInstall,
  useProviderHubRefreshCatalog,
  useProviderHubUninstall,
} from "@/apis/hooks/providerHub";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";

vi.mock("@/apis/raw", () => ({
  default: {
    providerHub: {
      // eslint-disable-next-line camelcase
      install: vi.fn().mockResolvedValue({ job_id: 21 }),
      // eslint-disable-next-line camelcase
      uninstall: vi.fn().mockResolvedValue({ job_id: 22 }),
      // eslint-disable-next-line camelcase
      refreshCatalog: vi.fn().mockResolvedValue({ job_id: 23 }),
    },
    system: { jobs: vi.fn().mockResolvedValue([]) },
  },
}));

const JOBS_KEY = [QueryKeys.System, QueryKeys.Jobs];

function setup() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { networkMode: "offlineFirst" },
    },
  });
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const invalidate = vi.spyOn(client, "invalidateQueries");
  return { client, wrapper, invalidate };
}

function report(client: QueryClient, id: number, status: string, message = "") {
  client.setQueryData(JOBS_KEY, [
    {
      /* eslint-disable camelcase */
      job_id: id,
      job_name: `Job ${id}`,
      status,
      progress_message: message,
      /* eslint-enable camelcase */
    },
  ]);
}

describe("Provider Hub jobs", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("keeps an install pending until its job completes", async () => {
    const { client, wrapper, invalidate } = setup();
    const { result } = renderHook(() => useProviderHubInstall(), { wrapper });

    let done = false;
    act(() => {
      void result.current
        .mutateAsync({ manifest: { provider_id: "examplehub" } })
        .then(() => {
          done = true;
        });
    });

    await waitFor(() => expect(api.providerHub.install).toHaveBeenCalled());
    expect(result.current.isPending).toBe(true);
    report(client, 21, "running");
    await Promise.resolve();
    expect(done).toBe(false);

    act(() => report(client, 21, "completed"));
    await waitFor(() => expect(done).toBe(true));
    await waitFor(() => expect(result.current.isPending).toBe(false));
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: [QueryKeys.ProviderHub],
    });
  });

  it("rejects an install with the reason its job failed with", async () => {
    const { client, wrapper } = setup();
    const { result } = renderHook(() => useProviderHubInstall(), { wrapper });

    let failure: unknown;
    act(() => {
      result.current
        .mutateAsync({ manifest: { provider_id: "examplehub" } })
        .catch((error: unknown) => {
          failure = error;
        });
    });
    await waitFor(() => expect(api.providerHub.install).toHaveBeenCalled());

    act(() =>
      report(
        client,
        21,
        "failed",
        "Could not install Example: bundle hash mismatch",
      ),
    );
    await waitFor(() => expect(failure).toBeInstanceOf(Error));
    expect((failure as Error).message).toBe(
      "Could not install Example: bundle hash mismatch",
    );
  });

  it("refreshes the Hub after an uninstall or a catalog refresh job, even a failed one", async () => {
    const { client, wrapper, invalidate } = setup();
    const { result: uninstall } = renderHook(() => useProviderHubUninstall(), {
      wrapper,
    });
    const { result: refresh } = renderHook(
      () => useProviderHubRefreshCatalog(),
      { wrapper },
    );

    act(() => {
      uninstall.current.mutate("examplehub");
      refresh.current.mutate();
    });
    await waitFor(() =>
      expect(api.providerHub.refreshCatalog).toHaveBeenCalled(),
    );
    expect(api.providerHub.uninstall).toHaveBeenCalledWith("examplehub");

    act(() =>
      client.setQueryData(JOBS_KEY, [
        /* eslint-disable camelcase */
        { job_id: 22, job_name: "Uninstall", status: "completed" },
        {
          job_id: 23,
          job_name: "Refresh",
          status: "failed",
          progress_message: "Could not refresh the provider catalog",
        },
        /* eslint-enable camelcase */
      ]),
    );

    await waitFor(() => expect(refresh.current.isError).toBe(true));
    await waitFor(() => expect(uninstall.current.isSuccess).toBe(true));
    expect(
      invalidate.mock.calls.filter(
        ([filters]) => filters?.queryKey?.[0] === QueryKeys.ProviderHub,
      ),
    ).toHaveLength(2);
  });
});
