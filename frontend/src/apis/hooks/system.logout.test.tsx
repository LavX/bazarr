import { PropsWithChildren } from "react";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@/tests";
import { useSystem } from "./system";

// The logout deletes the session server-side, and its response tells the
// browser to drop the session cookie. The reads the client still has mounted
// when the logout answers must not go out again before the navigation the
// logout performs unmounts them: a read that leaves while the signed-in
// session is still in the jar comes back with a fresh re-issued login in
// hand, and the next page finds the user signed in again. The rows mount a
// query beside the mutation the way SystemActions holds useSystem and
// useSystemSettings together, since a clearing the mutation's own completion
// re-render can observe is the one that restarts the reads.
const logout = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));

vi.mock("@/apis/raw", () => ({
  default: {
    system: {
      logout,
      login: vi.fn(),
      shutdown: vi.fn(),
      restart: vi.fn(),
    },
  },
}));

function makeClientAndWrapper() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0, networkMode: "offlineFirst" },
      mutations: { retry: false, networkMode: "offlineFirst" },
    },
  });
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

// One task past the logout: the clearing happened, and any restart of the
// reads it could still have caused showed up or never will.
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));

describe("useSystem logout", () => {
  beforeEach(() => {
    logout.mockClear();
  });

  it("does not restart the mounted reads when the logout succeeds", async () => {
    const { client, wrapper } = makeClientAndWrapper();
    const reads = vi.fn().mockResolvedValue({ signed: true });

    const { result } = renderHook(
      () => ({
        system: useSystem(),
        probe: useQuery({
          queryKey: ["logout-race", "settled"],
          queryFn: reads,
        }),
      }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.probe.isSuccess).toBe(true));
    expect(reads).toHaveBeenCalledTimes(1);

    result.current.system.logout();

    await waitFor(() => expect(logout).toHaveBeenCalledTimes(1));
    await settle();

    expect(reads).toHaveBeenCalledTimes(1);
    expect(client.getQueryCache().getAll()).toHaveLength(0);
  });

  it("drops a read still in flight when the logout succeeds", async () => {
    const { client, wrapper } = makeClientAndWrapper();
    let answer: ((value: { signed: boolean }) => void) | undefined;
    const reads = vi.fn(
      () =>
        new Promise<{ signed: boolean }>((resolve) => {
          answer = resolve;
        }),
    );
    const key = ["logout-race", "in-flight"];

    const { result } = renderHook(
      () => ({
        system: useSystem(),
        probe: useQuery({ queryKey: key, queryFn: reads }),
      }),
      { wrapper },
    );

    await waitFor(() => expect(reads).toHaveBeenCalledTimes(1));

    result.current.system.logout();

    await waitFor(() => expect(logout).toHaveBeenCalledTimes(1));
    await settle();

    // No read left again once the logout had answered.
    expect(reads).toHaveBeenCalledTimes(1);

    // The answer of the one that was already in flight lands nowhere, so
    // nothing carries the signed-in state past the logout.
    await act(async () => {
      answer?.({ signed: true });
    });
    expect(client.getQueryData(key)).toBeUndefined();
    expect(client.getQueryCache().getAll()).toHaveLength(0);
  });
});
