import { createElement, PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTestTranslator } from "@/apis/hooks/translator";
import type { SilentRequestConfig } from "@/apis/raw/base";
import {
  forgetPasswordConfirmation,
  markPasswordConfirmed,
  passwordConfirmWindowLive,
} from "@/modules/passwordConfirm";
import server from "@/tests/mocks/node";
import badgesApi from "./badges";
import client from "./client";
import { withInlineErrors } from "./inlineWrites";
import systemApi from "./system";

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

  it("hands a 500 with no error body to the caller", async () => {
    server.use(
      http.post(
        "/api/translator/test",
        () => new HttpResponse(null, { status: 500 }),
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
      ).rejects.toMatchObject({ response: { status: 500 } });
    });

    expect(onError).toHaveBeenCalledTimes(1);
    expect(showNotification).not.toHaveBeenCalled();
  });

  it("resolves a held-key answer from the translator test with its message", async () => {
    server.use(
      http.post("/api/translator/test", () =>
        HttpResponse.json({
          keyHeldByService: true,
          message: "The translator service holds its own key.",
        }),
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

    let data: unknown;
    await act(async () => {
      data = await result.current.mutateAsync({
        serviceUrl: "http://localhost:8990",
        apiKey: "",
      });
    });

    expect(data).toEqual({
      keyHeldByService: true,
      message: "The translator service holds its own key.",
    });
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

describe("sign-in announcements around logout", () => {
  let sessionChanges: boolean[] = [];
  const onAuth = (event: WindowEventMap["app-auth-changed"]) =>
    sessionChanges.push(event.detail.authenticated);

  beforeEach(() => {
    sessionChanges = [];
    client.bSignInBlocked = false;
    client.bIsAuthenticated = false;
    window.addEventListener("app-auth-changed", onAuth);
  });

  afterEach(() => {
    window.removeEventListener("app-auth-changed", onAuth);
    showNotification.mockClear();
  });

  it("a 2xx that left before logout does not announce sign-in", async () => {
    // The read is held until the logout has answered, then sends its 200.
    let openRead: () => void = () => undefined;
    const held = new Promise<void>((resolve) => {
      openRead = () => resolve();
    });
    server.use(
      http.get("/api/session-probe/probe", () =>
        HttpResponse.json({ message: "Sign in first" }, { status: 401 }),
      ),
      http.get("/api/session-probe/read", async () => {
        await held;
        return new HttpResponse(null, { status: 200 });
      }),
      http.post(
        "/api/system/account",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );

    await expect(client.axios.get("session-probe/probe")).rejects.toMatchObject(
      {
        response: { status: 401 },
      },
    );
    const read = client.axios.get("session-probe/read");
    const loggedOutAt = sessionChanges.length;

    await systemApi.logout();
    openRead();
    await read;

    expect(sessionChanges.slice(loggedOutAt)).toEqual([]);
  });

  it("a badges poll answered 2xx after logout does not announce sign-in", async () => {
    // The socket connects on an announcement of sign-in, so no announcement
    // here also means no reconnect. A 401 probe sits between the logout and
    // the poll, as a stale request answered by the server would.
    server.use(
      http.get("/api/badges", () => HttpResponse.json({})),
      http.get("/api/session-probe/probe", () =>
        HttpResponse.json({ message: "Sign in first" }, { status: 401 }),
      ),
      http.post(
        "/api/system/account",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );

    await systemApi.login("admin", "secret");
    await systemApi.logout();
    await expect(client.axios.get("session-probe/probe")).rejects.toMatchObject(
      {
        response: { status: 401 },
      },
    );
    const afterProbe = sessionChanges.length;

    await badgesApi.all();

    expect(sessionChanges.slice(afterProbe)).toEqual([]);
  });

  it("a login answered after a logout accepted behind it does not lift the block", async () => {
    // The login establishes the session, the logout revokes it, and the
    // login's own 204 is the last answer to arrive.
    let releaseLogin: () => void = () => undefined;
    const loginHeld = new Promise<void>((resolve) => {
      releaseLogin = () => resolve();
    });
    server.use(
      http.get("/api/badges", () => HttpResponse.json({})),
      http.post("/api/system/account", async ({ request }) => {
        if (new URL(request.url).searchParams.get("action") === "login") {
          await loginHeld;
        }
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const login = systemApi.login("admin", "secret");
    await systemApi.logout();
    const loggedOutAt = sessionChanges.length;
    releaseLogin();
    await login;

    expect(sessionChanges.slice(loggedOutAt)).toEqual([]);

    await badgesApi.all();
    expect(sessionChanges.slice(loggedOutAt)).toEqual([]);
  });

  it("two logins around one logout announce sign-in once, from the later one", async () => {
    // The first login is still pending when the logout is accepted. The second
    // is sent after it and answers first, so it lifts the block and announces.
    // The first one's late answer neither blocks again nor announces twice.
    let releaseFirst: () => void = () => undefined;
    const firstHeld = new Promise<void>((resolve) => {
      releaseFirst = () => resolve();
    });
    let logins = 0;
    server.use(
      http.get("/api/badges", () => HttpResponse.json({})),
      http.post("/api/system/account", async ({ request }) => {
        if (new URL(request.url).searchParams.get("action") === "login") {
          logins += 1;
          if (logins === 1) {
            await firstHeld;
          }
        }
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const first = systemApi.login("admin", "secret");
    await systemApi.logout();
    const loggedOutAt = sessionChanges.length;

    await systemApi.login("admin", "secret");
    expect(sessionChanges.slice(loggedOutAt)).toEqual([true]);

    releaseFirst();
    await first;
    expect(client.bSignInBlocked).toBe(false);
    expect(sessionChanges.slice(loggedOutAt)).toEqual([true]);

    await badgesApi.all();
    expect(sessionChanges.slice(loggedOutAt)).toEqual([true]);
  });

  it.each([403, 429])(
    "a login refused with %i leaves the block in place",
    async (status) => {
      server.use(
        http.get("/api/badges", () => HttpResponse.json({})),
        http.post("/api/system/account", ({ request }) =>
          new URL(request.url).searchParams.get("action") === "login"
            ? HttpResponse.json({ message: "Refused" }, { status })
            : new HttpResponse(null, { status: 204 }),
        ),
      );

      await systemApi.logout();
      const loggedOutAt = sessionChanges.length;
      await expect(systemApi.login("admin", "wrong")).rejects.toMatchObject({
        response: { status },
      });
      expect(client.bSignInBlocked).toBe(true);

      await badgesApi.all();
      expect(sessionChanges.slice(loggedOutAt)).toEqual([]);
    },
  );

  it("a 2xx that left while logout was pending does not announce sign-in once the sign-out is recorded", async () => {
    // The read leaves while the logout is pending, and is answered 200 only
    // after the logout has been accepted and recorded.
    let releaseLogout: () => void = () => undefined;
    const logoutHeld = new Promise<void>((resolve) => {
      releaseLogout = () => resolve();
    });
    let openRead: () => void = () => undefined;
    const readHeld = new Promise<void>((resolve) => {
      openRead = () => resolve();
    });
    server.use(
      http.get("/api/session-probe/probe", () =>
        HttpResponse.json({ message: "Sign in first" }, { status: 401 }),
      ),
      http.get("/api/session-probe/read", async () => {
        await readHeld;
        return new HttpResponse(null, { status: 200 });
      }),
      http.post("/api/system/account", async () => {
        await logoutHeld;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    await expect(client.axios.get("session-probe/probe")).rejects.toMatchObject(
      {
        response: { status: 401 },
      },
    );
    const loggedOutAt = sessionChanges.length;

    const logoutDone = systemApi.logout();
    const read = client.axios.get("session-probe/read");
    releaseLogout();
    await logoutDone;
    openRead();
    await read;

    expect(sessionChanges.slice(loggedOutAt)).toEqual([]);
  });

  it("a refused logout leaves a later 2xx free to announce sign-in", async () => {
    server.use(
      http.get("/api/session-probe/probe", () =>
        HttpResponse.json({ message: "Sign in first" }, { status: 401 }),
      ),
      http.get(
        "/api/session-probe/read",
        () => new HttpResponse(null, { status: 200 }),
      ),
      http.post("/api/system/account", () =>
        HttpResponse.json(
          {
            message:
              "Browser must be closed to invalidate basic authentication",
          },
          { status: 406 },
        ),
      ),
    );

    await expect(client.axios.get("session-probe/probe")).rejects.toMatchObject(
      {
        response: { status: 401 },
      },
    );
    await expect(systemApi.logout()).rejects.toMatchObject({
      response: { status: 406 },
    });
    const loggedOutAt = sessionChanges.length;
    await client.axios.get("session-probe/read");

    expect(sessionChanges.slice(loggedOutAt)).toEqual([true]);
  });

  it("a login answered 2xx after logout announces sign-in once, and a later read announces nothing further", async () => {
    server.use(
      http.get("/api/badges", () => HttpResponse.json({})),
      http.post(
        "/api/system/account",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );

    await systemApi.logout();
    const loggedOutAt = sessionChanges.length;

    await systemApi.login("admin", "secret");
    expect(sessionChanges.slice(loggedOutAt)).toEqual([true]);

    await badgesApi.all();
    expect(sessionChanges.slice(loggedOutAt)).toEqual([true]);
  });

  it("a login after a logout from a signed-in session announces sign-in again", async () => {
    server.use(
      http.post(
        "/api/system/account",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );

    await systemApi.login("admin", "secret");
    expect(sessionChanges).toEqual([true]);
    await systemApi.logout();
    const loggedOutAt = sessionChanges.length;

    await systemApi.login("admin", "secret");
    expect(sessionChanges.slice(loggedOutAt)).toEqual([true]);
  });

  it("a login lifts the block, so a 2xx after a later 401 announces sign-in again", async () => {
    server.use(
      http.get("/api/badges", () => HttpResponse.json({})),
      http.get("/api/session-probe/probe", () =>
        HttpResponse.json({ message: "Sign in first" }, { status: 401 }),
      ),
      http.post(
        "/api/system/account",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );

    await systemApi.logout();
    await systemApi.login("admin", "secret");
    expect(sessionChanges).toEqual([true]);

    await expect(client.axios.get("session-probe/probe")).rejects.toMatchObject(
      {
        response: { status: 401 },
      },
    );
    await badgesApi.all();

    expect(sessionChanges).toEqual([true, false, true]);
  });
});
