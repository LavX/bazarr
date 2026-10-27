/* eslint-disable camelcase */
import { PropsWithChildren } from "react";
import { MemoryRouter } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  useSportsBlacklistPagination,
  useSportsEvents,
  useSportsHistoryPagination,
  useSportsJob,
  useSportsLeaguesPagination,
  useSportsWantedPagination,
} from "@/apis/hooks/sports";
import sports, { type SportsEvent } from "@/apis/raw/sports";

vi.mock("@/apis/raw/sports", () => ({
  default: {
    list: vi.fn(),
    activity: vi.fn(),
    jobStatus: vi.fn(),
    events: vi.fn(),
  },
}));
vi.mock("@/apis/raw", () => ({ default: {} }));

const availability = vi.hoisted(() => ({
  enabled: undefined as boolean | undefined,
}));
const sportarrInstances = vi.hoisted(() => [
  { id: 42, kind: "sportarr", enabled: true },
]);
// useSportsAvailability now delegates to the generic per-kind helper, so the
// mock has to answer that too or every sports hook loses its availability.
vi.mock("../arrInstances", () => ({
  useArrInstances: () => ({ data: sportarrInstances, isLoading: false }),
  useArrKindAvailability: () => ({
    instances: sportarrInstances,
    enabled: availability.enabled === true,
    isLoading: availability.enabled === undefined,
  }),
}));
// The jobs cache the socket keeps current, which a sports job is followed through.
const jobsCache = vi.hoisted(() => ({ data: [] as unknown[] }));
vi.mock("../system", () => ({
  useSystemJobs: () => ({ data: jobsCache.data }),
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
  jobsCache.data = [];
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

function cachedJob(status: string, extra: object = {}) {
  return {
    job_id: 9,
    job_name: "Searching",
    status,
    progress_message: status === "running" ? "Running" : "Done",
    ...extra,
  };
}

it("follows a job through the jobs cache and never asks for it while it runs", async () => {
  availability.enabled = true;
  jobsCache.data = [cachedJob("running")];
  const status = vi.spyOn(sports, "jobStatus");
  const { result } = renderHook(() => useSportsJob(9, 42), { wrapper });
  expect(result.current.data).toMatchObject({
    status: "running",
    message: "Running",
    cancelled: false,
    result: null,
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
  });
  // Nothing polls beside the jobs socket.
  expect(status).not.toHaveBeenCalled();
});

it("does not follow a job of another backend run that shares the id", async () => {
  availability.enabled = true;
  jobsCache.data = [cachedJob("completed", { session: "run-b" })];
  const status = vi.spyOn(sports, "jobStatus");
  const { result } = renderHook(() => useSportsJob(9, 42, "run-a"), {
    wrapper,
  });
  expect(result.current.data).toBeUndefined();
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 300));
  });
  expect(status).not.toHaveBeenCalled();

  jobsCache.data = [cachedJob("running", { session: "run-a" })];
  const { result: own } = renderHook(() => useSportsJob(9, 42, "run-a"), {
    wrapper,
  });
  expect(own.current.data).toMatchObject({ status: "running" });
});

it("reads what the job produced one time, once it has ended", async () => {
  availability.enabled = true;
  jobsCache.data = [cachedJob("running")];
  const status = vi.spyOn(sports, "jobStatus").mockResolvedValue({
    job_id: 9,
    arr_instance_id: 42,
    cancelled: false,
    status: "completed",
    message: "Done",
    result: { file_status: "preserved" },
  });
  const { result, rerender } = renderHook(() => useSportsJob(9, 42), {
    wrapper,
  });
  jobsCache.data = [cachedJob("completed")];
  rerender();
  await waitFor(() =>
    expect(result.current.data?.result?.file_status).toBe("preserved"),
  );
  expect(result.current.data?.status).toBe("completed");
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 1200));
  });
  expect(status).toHaveBeenCalledTimes(1);
});

it("says a stopped job was cancelled and a failed one why", async () => {
  availability.enabled = true;
  vi.spyOn(sports, "jobStatus").mockResolvedValue({
    job_id: 9,
    arr_instance_id: 42,
    cancelled: true,
    status: "cancelled",
    message: "",
    result: null,
  });
  jobsCache.data = [cachedJob("cancelled")];
  const { result: stopped } = renderHook(() => useSportsJob(9, 42), {
    wrapper,
  });
  expect(stopped.current.data?.cancelled).toBe(true);

  jobsCache.data = [
    cachedJob("failed", {
      error: { reason: "x", message: "The provider refused." },
    }),
  ];
  const { result: failed } = renderHook(() => useSportsJob(9, 42), { wrapper });
  expect(failed.current.data?.message).toBe("The provider refused.");
});

it("reports a job its owner cannot read, and does not ask again", async () => {
  availability.enabled = true;
  jobsCache.data = [cachedJob("completed")];
  const status = vi
    .spyOn(sports, "jobStatus")
    .mockRejectedValue({ response: { status: 404 } });
  const { result } = renderHook(() => useSportsJob(9, 42), { wrapper });
  await waitFor(() => expect(result.current.isError).toBe(true));
  const requests = status.mock.calls.length;
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 1200));
  });
  expect(status).toHaveBeenCalledTimes(requests);
});

it("polls the owned event listing once for running syncs and stops when complete", async () => {
  availability.enabled = true;
  const row: SportsEvent = {
    title: "Event",
    eventDate: null,
    broadcastDate: null,
    hasFile: true,
    monitored: true,
    mapped_path: "/sports/event.mkv",
    path: "/sports/event.mkv",
    file_size: 100,
    profileId: null,
    audio_language: [],
    subtitles: [["en", "/sports/event.en.srt", 40]],
    missing_subtitles: [],
    sportarrEventId: 9,
    file_id: 71,
    partNumber: null,
    partName: null,
    season: null,
    episode: null,
    id: 61,
    arr_instance_id: 42,
    league_id: 51,
    sync_status: {
      en: {
        synced: false,
        confirmed: false,
        editedAfterSync: false,
        lastModified: 100,
        lastSyncTimestamp: null,
        jobStatus: "running" as const,
      },
    },
  };
  const events = vi
    .spyOn(sports, "events")
    .mockResolvedValueOnce({
      data: [row],
      total: 1,
    })
    .mockResolvedValue({ data: [], total: 0 });
  const { result } = renderHook(() => useSportsEvents(51, 42), { wrapper });
  await waitFor(() => expect(result.current.data?.total).toBe(0), {
    timeout: 3000,
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 2200));
  });
  expect(events).toHaveBeenCalledTimes(2);
  expect(events).toHaveBeenLastCalledWith(51, 42, 0, -1);
});
