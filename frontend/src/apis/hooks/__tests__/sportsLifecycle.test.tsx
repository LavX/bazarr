/* eslint-disable camelcase */
import { PropsWithChildren } from "react";
import { MemoryRouter } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  useSportsBlacklistPagination,
  useSportsHistoryPagination,
  useSportsJob,
  useSportsLeaguesPagination,
  useSportsWantedPagination,
} from "@/apis/hooks/sports";
import sports from "@/apis/raw/sports";

vi.mock("@/apis/raw/sports", () => ({
  default: { list: vi.fn(), activity: vi.fn(), jobStatus: vi.fn() },
}));
vi.mock("@/apis/raw", () => ({ default: {} }));

const availability = vi.hoisted(() => ({
  enabled: undefined as boolean | undefined,
}));
vi.mock("../arrInstances", () => ({
  useArrInstances: () => ({
    data: [{ id: 42, kind: "sportarr", enabled: true }],
    isLoading: false,
  }),
}));
vi.mock("../system", () => ({
  useSystemSettings: () => ({
    data:
      availability.enabled === undefined
        ? undefined
        : {
            general: { use_sportarr: availability.enabled },
          },
    isLoading: availability.enabled === undefined,
  }),
}));
vi.mock("@/utilities/storage", () => ({ usePageSize: () => 25 }));

function wrapper({ children }: PropsWithChildren) {
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}
let client: QueryClient;
beforeEach(() => {
  vi.resetAllMocks();
  availability.enabled = undefined;
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(() => {
  client.clear();
  vi.restoreAllMocks();
});

it.each([
  ["leagues", () => useSportsLeaguesPagination()],
  ["wanted", () => useSportsWantedPagination({})],
  ["history", () => useSportsHistoryPagination({})],
  ["blacklist", () => useSportsBlacklistPagination({})],
] as const)(
  "fetches %s when settings arrive after the final owner IDs",
  async (_, usePage) => {
    const list = vi
      .spyOn(sports, "list")
      .mockResolvedValue({ data: [], total: 7 });
    const activity = vi
      .spyOn(sports, "activity")
      .mockResolvedValue({ data: [], total: 7 });
    const { result, rerender } = renderHook(
      () => {
        const page = usePage();
        return { isSuccess: page.isSuccess, total: page.data?.total };
      },
      { wrapper },
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.isSuccess).toBe(false);
    expect(list).not.toHaveBeenCalled();
    expect(activity).not.toHaveBeenCalled();
    availability.enabled = true;
    rerender();
    await waitFor(() => expect(result.current.total).toBe(7));
  },
);

it("fetches after enabling the master toggle without changing owner IDs", async () => {
  availability.enabled = false;
  const list = vi
    .spyOn(sports, "list")
    .mockResolvedValue({ data: [], total: 5 });
  const { result, rerender } = renderHook(() => useSportsLeaguesPagination(), {
    wrapper,
  });
  await act(async () => {
    await Promise.resolve();
  });
  expect(list).not.toHaveBeenCalled();
  availability.enabled = true;
  rerender();
  await waitFor(() => expect(result.current.data?.total).toBe(5));
});

it.each([false, true])(
  "stops polling an unavailable job, previously running: %s",
  async (running) => {
    availability.enabled = true;
    const status = vi.spyOn(sports, "jobStatus");
    if (running)
      status.mockResolvedValueOnce({
        job_id: 9,
        arr_instance_id: 42,
        cancelled: false,
        result: null,
        status: "running",
        message: "Running",
      });
    status.mockRejectedValue({ response: { status: 404 } });
    const { result } = renderHook(() => useSportsJob(9, 42), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true), {
      timeout: 2000,
    });
    const requests = status.mock.calls.length;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2200));
    });
    expect(status).toHaveBeenCalledTimes(requests);
  },
);

it("continues polling running work and stops when it completes", async () => {
  availability.enabled = true;
  const status = vi
    .spyOn(sports, "jobStatus")
    .mockResolvedValueOnce({
      job_id: 9,
      arr_instance_id: 42,
      cancelled: false,
      result: null,
      status: "running",
      message: "Running",
    })
    .mockResolvedValue({
      job_id: 9,
      arr_instance_id: 42,
      cancelled: false,
      result: null,
      status: "completed",
      message: "Done",
    });
  const { result } = renderHook(() => useSportsJob(9, 42), { wrapper });
  await waitFor(() => expect(result.current.data?.status).toBe("completed"), {
    timeout: 2000,
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 1200));
  });
  expect(status).toHaveBeenCalledTimes(2);
});
