/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { createMemoryRouter, RouterProvider } from "react-router";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import Discover from ".";

// No Discover test registered a handler for the provider-hub endpoint, so the
// Hub query always failed, readiness was always "unknown", and neither branch
// of this notice was ever rendered in the suite. The copy went out unguarded,
// which is how it came to tell a reader that an installed provider is built in
// on evidence that does not establish it.

function install(overrides: Record<string, unknown> = {}) {
  return {
    provider_id: "catalog-example",
    name: "Catalog Example",
    state: "active",
    trusted: true,
    pending_restart: false,
    origin: "catalog",
    ...overrides,
  };
}

function serve(
  enabled: string[],
  installs: Record<string, unknown>[] | "pending",
) {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", enabled_providers: enabled },
      }),
    ),
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { code2: "en", code3: "eng", name: "English", enabled: true },
      ]),
    ),
    http.get("/api/system/languages/profiles", () => HttpResponse.json([])),
    http.get("/api/provider-hub/providers", () =>
      installs === "pending"
        ? new Promise<never>(() => undefined)
        : HttpResponse.json({ data: installs }),
    ),
  );
}

function open() {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <Discover /> },
      { path: "/subtitle-hub", element: <div>Subtitle Hub</div> },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
}

// The page carries a second live region for search progress, so the notice is
// identified by the Hub link only it offers before a search.
function readinessNotice() {
  return (
    screen
      .queryAllByRole("status")
      .find((region) =>
        /Open the Subtitle Hub/.test(region.textContent ?? ""),
      ) ?? null
  );
}

async function findReadinessNotice() {
  await waitFor(() => expect(readinessNotice()).not.toBeNull());
  return readinessNotice() as HTMLElement;
}

async function settle() {
  await screen.findByRole("button", { name: /Find subtitles/ });
  await screen.findByRole("combobox", { name: /Subtitle language/ });
}

beforeEach(() => {
  localStorage.clear();
  queryClient.clear();
});

it("says nothing is enabled when nothing is enabled", async () => {
  serve([], []);
  open();
  expect(await findReadinessNotice()).toHaveTextContent(
    /No subtitle provider is enabled/,
  );
});

it("stays silent while a searchable catalog provider is enabled", async () => {
  serve(["catalog-example"], [install()]);
  open();
  await settle();
  expect(readinessNotice()).toBeNull();
});

it("calls a provider built in only when the Hub has never heard of it", async () => {
  serve(["bsplayer"], []);
  open();
  const notice = await findReadinessNotice();
  expect(notice).toHaveTextContent(/bsplayer is not a catalog provider/);
  expect(notice).toHaveTextContent(/installed, trusted catalog providers only/);
});

// The three installed states the old copy called built-in and could not know.
it.each([
  [
    "waiting for a restart",
    { pending_restart: true },
    /Catalog Example is waiting for a restart/,
  ],
  ["not trusted", { trusted: false }, /Catalog Example is not trusted/],
  ["not active", { state: "error" }, /Catalog Example is not active \(error\)/],
])(
  "does not call a provider built in when it is installed but %s",
  async (_name, overrides, expected) => {
    serve(["catalog-example"], [install(overrides)]);
    open();
    const notice = await findReadinessNotice();
    expect(notice).toHaveTextContent(expected);
    // The claim a reader must never be given about a provider they installed
    // from the very marketplace this notice links to.
    expect(notice).not.toHaveTextContent(/built-in/);
    expect(notice).not.toHaveTextContent(/not a catalog provider/);
  },
);

it("gives each provider the reason that actually applies to it", async () => {
  serve(["bsplayer", "catalog-example"], [install({ pending_restart: true })]);
  open();
  const notice = await findReadinessNotice();
  expect(notice).toHaveTextContent(/bsplayer is not a catalog provider/);
  expect(notice).toHaveTextContent(/Catalog Example is waiting for a restart/);
});

// Grouping several providers under one reason is the ordinary case, not the
// edge one: a stock install has three enabled built-ins, and the live capture
// read "bsplayer, gestdown and yifysubtitles are not a catalog provider".
it("counts correctly when several providers share one reason", async () => {
  serve(["bsplayer", "gestdown", "yifysubtitles"], []);
  open();
  const notice = await findReadinessNotice();
  expect(notice).toHaveTextContent(
    /bsplayer, gestdown and yifysubtitles are not catalog providers\./,
  );
  expect(notice).not.toHaveTextContent(/are not a catalog provider/);
});

it("keeps each group on its own number", async () => {
  serve(
    ["bsplayer", "catalog-example", "catalog-two"],
    [
      install({ trusted: false }),
      install({
        provider_id: "catalog-two",
        name: "Catalog Two",
        trusted: false,
      }),
    ],
  );
  open();
  const notice = await findReadinessNotice();
  expect(notice).toHaveTextContent(/bsplayer is not a catalog provider\./);
  expect(notice).toHaveTextContent(
    /Catalog Example and Catalog Two are not trusted\./,
  );
});

it("names a provider the way the Hub names it, not by its raw id", async () => {
  serve(["catalog-example"], [install({ trusted: false })]);
  open();
  const notice = await findReadinessNotice();
  expect(notice).toHaveTextContent(/Catalog Example/);
  expect(notice).not.toHaveTextContent(/catalog-example/);
});

it("withholds the notice entirely while the Hub has not answered", async () => {
  serve(["bsplayer"], "pending");
  open();
  await settle();
  // Naming a reason before the Hub has answered would be a guess, so nothing
  // is said at all.
  expect(readinessNotice()).toBeNull();
});
