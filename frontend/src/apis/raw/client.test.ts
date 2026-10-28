import { createElement, PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useTestTranslator } from "@/apis/hooks/translator";
import type { SilentRequestConfig } from "@/apis/raw/base";
import {
  forgetPasswordConfirmation,
  markPasswordConfirmed,
  passwordConfirmWindowLive,
} from "@/modules/passwordConfirm";
import server from "@/tests/mocks/node";
import client from "./client";
import { withInlineErrors } from "./inlineWrites";

const showNotification = vi.fn();

vi.mock("@mantine/notifications", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@mantine/notifications")>()),
  showNotification: (args: unknown) => showNotification(args),
}));

// Real requests through the real client and its interceptors; MSW answers.
function failingWrites() {
  server.use(
    http.post("/api/inline-probe/a", () =>
      HttpResponse.json({ message: "Bad value" }, { status: 400 }),
    ),
    http.post("/api/inline-probe/b", () =>
      HttpResponse.json({ message: "Bad value" }, { status: 400 }),
    ),
    http.get("/api/inline-probe/a", () =>
      HttpResponse.json({ message: "Bad value" }, { status: 400 }),
    ),
  );
}

const post = (path: string) => client.axios.post(path).catch(() => undefined);

describe("inline write errors, per request", () => {
  afterEach(() => showNotification.mockClear());

  it("toasts a failed write nobody claimed", async () => {
    failingWrites();
    await post("inline-probe/a");
    expect(showNotification).toHaveBeenCalledTimes(1);
  });

  it("skips the toast for the request a claimed call sends", async () => {
    failingWrites();
    await withInlineErrors(() => post("inline-probe/a"));
    expect(showNotification).not.toHaveBeenCalled();
  });

  it("leaves a refused translator test to its caller", async () => {
    server.use(
      http.post("/api/translator/test", () =>
        HttpResponse.json(
          { detail: "reason text", message: "Request refused" },
          { status: 400 },
        ),
      ),
    );
    const queryClient = new QueryClient({
      defaultOptions: {
        mutations: { retry: false, networkMode: "offlineFirst" },
      },
    });
    const wrapper = ({ children }: PropsWithChildren) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useTestTranslator(), { wrapper });
    const onError = vi.fn();

    await act(async () => {
      await expect(
        result.current.mutateAsync(
          { serviceUrl: "http://localhost:8990" },
          { onError },
        ),
      ).rejects.toMatchObject({
        response: { status: 400, data: { detail: "reason text" } },
      });
    });

    expect(onError).toHaveBeenCalledTimes(1);
    expect(showNotification).not.toHaveBeenCalled();
  });

  it("still toasts an unclaimed write sent while a claimed one is in flight", async () => {
    // The flag was once global for as long as setup was mounted, and a Plex
    // sign-out or a refused restart with no handler of its own then failed in
    // silence. Only the request the claiming call sent is covered.
    failingWrites();
    const claimed = withInlineErrors(() => post("inline-probe/a"));
    const unclaimed = post("inline-probe/b");
    await Promise.all([claimed, unclaimed]);
    expect(showNotification).toHaveBeenCalledTimes(1);
  });

  it("does not cover a request sent after the claimed call awaited", async () => {
    failingWrites();
    await withInlineErrors(async () => {
      await Promise.resolve();
      return post("inline-probe/b");
    });
    expect(showNotification).toHaveBeenCalledTimes(1);
  });

  it("covers a claimed read the same way", async () => {
    failingWrites();
    await withInlineErrors(() =>
      client.axios.get("inline-probe/a").catch(() => undefined),
    );
    expect(showNotification).not.toHaveBeenCalled();
  });
});

describe("password confirmation answers", () => {
  afterEach(() => {
    showNotification.mockClear();
    forgetPasswordConfirmation();
  });

  const silentPost = (path: string) =>
    client.axios
      .post(path, undefined, { silentError: true } as SilentRequestConfig)
      .catch(() => undefined);

  it("forgets the remembered confirmation when a gated call is refused", async () => {
    server.use(
      http.post("/api/gated-probe", () =>
        HttpResponse.json(
          { code: "confirm_password", message: "Confirm your password" },
          { status: 403 },
        ),
      ),
    );
    markPasswordConfirmed();
    expect(passwordConfirmWindowLive()).toBe(true);

    await post("gated-probe");

    expect(passwordConfirmWindowLive()).toBe(false);
  });

  it("keeps the confirmation for a 403 that is not a confirmation refusal", async () => {
    server.use(
      http.post("/api/gated-probe", () =>
        HttpResponse.json({ message: "Forbidden" }, { status: 403 }),
      ),
    );
    markPasswordConfirmed();

    await post("gated-probe");

    expect(passwordConfirmWindowLive()).toBe(true);
  });

  it("stays quiet for a silent request's 403 and 429 only", async () => {
    server.use(
      http.post("/api/silent-probe/403", () =>
        HttpResponse.json(
          { message: "Authentication failed" },
          { status: 403 },
        ),
      ),
      http.post("/api/silent-probe/429", () =>
        HttpResponse.json({ message: "Too many" }, { status: 429 }),
      ),
      http.post("/api/silent-probe/500", () =>
        HttpResponse.json({ message: "Broken" }, { status: 500 }),
      ),
    );

    await silentPost("silent-probe/403");
    await silentPost("silent-probe/429");
    expect(showNotification).not.toHaveBeenCalled();

    await silentPost("silent-probe/500");
    expect(showNotification).toHaveBeenCalledTimes(1);
  });

  it("still signs out on a silent request's 401", async () => {
    server.use(
      http.post("/api/silent-probe/401", () =>
        HttpResponse.json({ message: "Sign in first" }, { status: 401 }),
      ),
    );

    client.bIsAuthenticated = true;

    await silentPost("silent-probe/401");

    expect(client.bIsAuthenticated).toBe(false);
    expect(showNotification).not.toHaveBeenCalled();
  });
});
