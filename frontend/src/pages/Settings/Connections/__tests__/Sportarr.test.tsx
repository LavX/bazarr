/* eslint-disable camelcase */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ArrInstance } from "@/apis/raw/arrInstances";
import SettingsConnectionsView from "@/pages/Settings/Connections";
import InstanceCard from "@/pages/Settings/Connections/InstanceCard";
import InstanceFormModal from "@/pages/Settings/Connections/InstanceFormModal";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import {
  makeInstance,
  radarrDefault,
  sonarrDefault,
  sportarr,
  sportarrSibling,
  sportsProfile,
} from "./fixtures";
import styles from "@/pages/Settings/Connections/Connections.module.scss";

function cardNamed(name: string) {
  const actions = screen.getByRole("button", {
    name: `More actions for ${name}`,
  });
  // Cards have no region role, so use their named action to scope badge and switch assertions.
  // eslint-disable-next-line testing-library/no-node-access
  const card = actions.closest<HTMLElement>(`.${styles.card}`);
  if (!card) throw new Error(`Card missing for ${name}`);
  return card;
}

async function expectOtherKindDefaults(
  user: ReturnType<typeof userEvent.setup>,
) {
  for (const [kind, name, host] of [
    ["Sonarr", "Main Sonarr", "http://192.168.1.20:8989"],
    ["Radarr", "Main Radarr", "http://192.168.1.30:7878"],
  ]) {
    await user.click(screen.getByRole("tab", { name: kind }));
    expect(await screen.findByText(name)).toBeInTheDocument();
    const card = within(cardNamed(name));
    expect(card.getByText("Default")).toBeInTheDocument();
    expect(card.queryByText("Disabled")).toBeNull();
    expect(
      card.getByRole("switch", { name: "Disable instance" }),
    ).toBeChecked();
    expect(card.getByText(host)).toBeInTheDocument();
    expect(screen.getAllByText("Default")).toHaveLength(1);
  }
  await user.click(screen.getByRole("tab", { name: "Sportarr" }));
}

async function settingsLoaded() {
  // The page mounts before the settings query resolves. That resolution
  // re-renders the tree and closes any card Menu already open, so wait for the
  // master toggle to reflect the loaded value before touching a card.
  await waitFor(() =>
    expect(screen.getByRole("switch", { name: "Enabled" })).toBeChecked(),
  );
}

describe("Sportarr Connections", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/#sportarr");
    server.use(
      http.get("/api/system/languages/profiles", () =>
        HttpResponse.json([sportsProfile]),
      ),
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: {
            theme: "auto",
            use_sonarr: true,
            use_radarr: true,
            use_sportarr: true,
            minimum_score_sports: 70,
            path_mappings_sports: [],
          },
          sportarr: {
            excluded_tags: [],
            excluded_sports: [],
            only_monitored: false,
            search_on_sync: true,
          },
        }),
      ),
    );
  });

  afterEach(() => {
    window.history.replaceState({}, "", "/");
  });

  it("restores the Sportarr tab and offers only connection actions", async () => {
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
    );
    customRender(<SettingsConnectionsView />);

    expect(await screen.findByText("Main Sportarr")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Sportarr" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("button", { name: "Test" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Edit" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /webhook/i })).toBeNull();
    expect(screen.queryByText(/webhook/i)).toBeNull();
    expect(
      screen.queryByRole("link", { name: /sports|movies|series/i }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: /sync now/i })).toBeNull();
    expect(screen.queryByText("Use Sonarr")).toBeNull();
    expect(screen.queryByText("Use Radarr")).toBeNull();
  });

  it("creates Sportarr with its default port and the complete connection body", async () => {
    const user = userEvent.setup();
    const createdSportarr = makeInstance({
      id: 42,
      kind: "sportarr",
      stable_key: "main-sportarr",
      name: "Main Sportarr",
      display_name: "Main Sportarr",
      ip: "sports.local",
      port: 1867,
      is_default: true,
      verify_ssl: true,
      http_timeout: 60,
      media_defaults: {},
    });
    const beforeCreate = [sonarrDefault, radarrDefault];
    const afterCreate = [sonarrDefault, radarrDefault, createdSportarr];
    let createdBody: unknown;
    let saved: ArrInstance[] = beforeCreate;
    server.use(
      http.get("/api/system/arr-instances", () => HttpResponse.json(saved)),
      http.post("/api/system/arr-instances", async ({ request }) => {
        createdBody = await request.json();
        saved = afterCreate;
        return HttpResponse.json(createdSportarr, { status: 201 });
      }),
    );
    customRender(<SettingsConnectionsView />);
    await user.click(
      await screen.findByRole("button", { name: /add your first Sportarr/i }),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Add Sportarr instance" }),
    );
    expect(await dialog.findByRole("textbox", { name: "Port" })).toHaveValue(
      "1867",
    );
    expect(dialog.getByRole("radio", { name: "Sportarr" })).toBeChecked();
    expect(
      dialog.getByRole("combobox", {
        name: "Profile for newly synced leagues",
      }),
    ).toHaveValue("No default profile");
    await user.type(
      dialog.getByRole("textbox", { name: "Name" }),
      "Main Sportarr",
    );
    await user.type(
      dialog.getByRole("textbox", { name: "Address" }),
      "sports.local",
    );
    await user.type(dialog.getByLabelText("API Key"), "new-test-key");
    await user.click(dialog.getByRole("button", { name: "Add instance" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(createdBody).toEqual({
      kind: "sportarr",
      name: "Main Sportarr",
      ip: "sports.local",
      port: 1867,
      base_url: "",
      ssl: false,
      verify_ssl: true,
      http_timeout: 60,
      enabled: true,
      api_key: "new-test-key",
    });
    expect(createdBody).not.toHaveProperty("is_default");
    expect(createdBody).not.toHaveProperty("media_defaults");
    expect(await screen.findByText("Main Sportarr")).toBeInTheDocument();
    const card = within(cardNamed("Main Sportarr"));
    expect(card.getByText("Default")).toBeInTheDocument();
    expect(
      card.getByRole("switch", { name: "Disable instance" }),
    ).toBeChecked();
    expect(card.getByText("http://sports.local:1867")).toBeInTheDocument();
    expect(screen.getAllByText("Default")).toHaveLength(1);
    await user.click(card.getByRole("button", { name: "Edit" }));
    const reopened = within(
      await screen.findByRole("dialog", { name: "Edit Main Sportarr" }),
    );
    expect(
      await reopened.findByRole("combobox", {
        name: "Profile for newly synced leagues",
      }),
    ).toHaveValue("No default profile");
    expect(
      reopened.getByRole("switch", { name: "Default Sportarr instance" }),
    ).toBeChecked();
    expect(reopened.getByRole("textbox", { name: "Address" })).toHaveValue(
      "sports.local",
    );
    await user.click(reopened.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await expectOtherKindDefaults(user);
  });

  it("changes untouched default ports across all kinds and preserves a custom port", async () => {
    const user = userEvent.setup();
    customRender(
      <InstanceFormModal
        opened
        kind="sonarr"
        instance={null}
        onClose={vi.fn()}
      />,
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Add Sonarr instance" }),
    );
    const port = await dialog.findByRole("textbox", { name: "Port" });
    expect(port).toHaveValue("8989");
    for (const [label, expected] of [
      ["Sportarr", "1867"],
      ["Radarr", "7878"],
      ["Sonarr", "8989"],
    ]) {
      await user.click(dialog.getByRole("radio", { name: label }));
      expect(port).toHaveValue(expected);
    }
    await user.clear(port);
    await user.type(port, "9123");
    for (const label of ["Sportarr", "Radarr", "Sonarr"]) {
      await user.click(dialog.getByRole("radio", { name: label }));
      expect(port).toHaveValue("9123");
    }
  });

  it("edits and tests with the stored key without sending it or changing kind", async () => {
    const user = userEvent.setup();
    const editedSportarr = {
      ...sportarr,
      name: "Sports server",
      display_name: "Sports server",
      ip: "sports.local",
      port: 1967,
      base_url: "/league-proxy",
      ssl: true,
      verify_ssl: true,
      http_timeout: 45,
    };
    let updateBody: unknown;
    let testBody: unknown;
    let saved = sportarr;
    server.use(
      http.get("/api/system/arr-instances", () => HttpResponse.json([saved])),
      http.patch("/api/system/arr-instances/42", async ({ request }) => {
        updateBody = await request.json();
        saved = editedSportarr;
        return HttpResponse.json(editedSportarr);
      }),
      http.post("/api/system/arr-instances/42/test", async ({ request }) => {
        testBody = await request.json();
        return HttpResponse.json({
          ok: true,
          app_name: "Sportarr",
          version: "1.0",
        });
      }),
    );
    customRender(<SettingsConnectionsView />);
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = within(
      await screen.findByRole("dialog", { name: "Edit Main Sportarr" }),
    );
    expect(
      await dialog.findByRole("radio", { name: "Keep current key" }),
    ).toBeChecked();
    expect(dialog.queryByRole("radio", { name: "Sportarr" })).toBeNull();
    expect(dialog.queryByLabelText("New API key")).toBeNull();
    expect(
      dialog.getByRole("button", { name: /apply to unset/i }),
    ).toBeEnabled();
    expect(dialog.queryByText(/Movies list/)).toBeNull();
    await waitFor(() =>
      expect(
        dialog.getByRole("combobox", {
          name: "Profile for newly synced leagues",
        }),
      ).toHaveValue("English Sports"),
    );

    await user.clear(dialog.getByRole("textbox", { name: "Name" }));
    await user.type(
      dialog.getByRole("textbox", { name: "Name" }),
      "Sports server",
    );
    await user.clear(dialog.getByRole("textbox", { name: "Address" }));
    await user.type(
      dialog.getByRole("textbox", { name: "Address" }),
      "sports.local",
    );
    await user.clear(dialog.getByRole("textbox", { name: "Port" }));
    await user.type(dialog.getByRole("textbox", { name: "Port" }), "1967");
    await user.type(
      dialog.getByRole("textbox", { name: "Base URL" }),
      "/league-proxy/",
    );
    await user.clear(dialog.getByRole("textbox", { name: "Timeout" }));
    await user.type(dialog.getByRole("textbox", { name: "Timeout" }), "45");
    await user.click(dialog.getByRole("switch", { name: "Use SSL" }));
    await user.click(
      dialog.getByRole("switch", { name: "Verify certificate" }),
    );
    await user.click(dialog.getByRole("button", { name: "Test Connection" }));
    expect(
      await dialog.findByText("Connected to Sportarr"),
    ).toBeInTheDocument();
    expect(testBody).toEqual({
      ip: "sports.local",
      port: 1967,
      base_url: "/league-proxy",
      ssl: true,
      verify_ssl: true,
      http_timeout: 45,
    });
    expect(testBody).not.toHaveProperty("api_key");
    expect(testBody).not.toHaveProperty("kind");
    await user.click(dialog.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(updateBody).toEqual({
      name: "Sports server",
      ip: "sports.local",
      port: 1967,
      base_url: "/league-proxy",
      ssl: true,
      verify_ssl: true,
      http_timeout: 45,
      enabled: true,
      is_default: true,
      subtitle_settings: {},
      media_defaults: { default_enabled: true, default_profile: 3 },
    });
    expect(updateBody).not.toHaveProperty("api_key");
    expect(updateBody).not.toHaveProperty("clear_api_key");
    expect(updateBody).not.toHaveProperty("kind");
    expect(await screen.findByText("Sports server")).toBeInTheDocument();
    expect(
      within(cardNamed("Sports server")).getByText(
        "https://sports.local:1967/league-proxy",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("(Main Sportarr)")).toBeNull();
    await user.click(
      within(cardNamed("Sports server")).getByRole("button", { name: "Edit" }),
    );
    const reopened = within(
      await screen.findByRole("dialog", { name: "Edit Sports server" }),
    );
    await waitFor(() =>
      expect(
        reopened.getByRole("combobox", {
          name: "Profile for newly synced leagues",
        }),
      ).toHaveValue("English Sports"),
    );
    expect(
      reopened.getByRole("radio", { name: "Keep current key" }),
    ).toBeChecked();
    expect(reopened.queryByRole("radio", { name: "Sportarr" })).toBeNull();
    expect(reopened.getByRole("textbox", { name: "Name" })).toHaveValue(
      "Sports server",
    );
    expect(reopened.getByRole("textbox", { name: "Address" })).toHaveValue(
      "sports.local",
    );
    expect(reopened.getByRole("textbox", { name: "Port" })).toHaveValue("1967");
    expect(reopened.getByRole("textbox", { name: "Base URL" })).toHaveValue(
      "league-proxy",
    );
    expect(reopened.getByRole("textbox", { name: "Timeout" })).toHaveValue(
      "45",
    );
    expect(reopened.getByRole("switch", { name: "Use SSL" })).toBeChecked();
    expect(
      reopened.getByRole("switch", { name: "Verify certificate" }),
    ).toBeChecked();
  });

  it("shows a wrong stored-key error after a card test without sending a key", async () => {
    const user = userEvent.setup();
    let body: unknown;
    server.use(
      http.post("/api/system/arr-instances/42/test", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({
          ok: false,
          error: "unauthorized",
          message: "Sportarr rejected the API key",
        });
      }),
    );
    customRender(
      <InstanceCard instance={sportarr} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    await user.click(screen.getByRole("button", { name: "Test" }));
    expect(
      await screen.findByText("Sportarr rejected the API key"),
    ).toBeInTheDocument();
    expect(body).toEqual({});
  });

  it("promotes a Sportarr instance and persists disabled and enabled card states", async () => {
    const user = userEvent.setup();
    const promoted = { ...sportarrSibling, is_default: true };
    const demoted = { ...sportarr, is_default: false };
    const disabled = { ...sportarrSibling, enabled: false, is_default: false };
    const beforePromotion = [
      sonarrDefault,
      radarrDefault,
      sportarr,
      sportarrSibling,
    ];
    const afterPromotion = [sonarrDefault, radarrDefault, demoted, promoted];
    const afterDisable = [sonarrDefault, radarrDefault, sportarr, disabled];
    const afterEnable = [
      sonarrDefault,
      radarrDefault,
      sportarr,
      sportarrSibling,
    ];
    let saved = beforePromotion;
    const updates: unknown[] = [];
    server.use(
      http.get("/api/system/arr-instances", () => HttpResponse.json(saved)),
      http.patch("/api/system/arr-instances/43", async ({ request }) => {
        updates.push(await request.json());
        saved = afterPromotion;
        return HttpResponse.json(promoted);
      }),
    );
    customRender(<SettingsConnectionsView />);
    expect(await screen.findByText("Sportarr Archive")).toBeInTheDocument();
    expect(
      within(cardNamed("Main Sportarr")).getByText("Default"),
    ).toBeInTheDocument();
    expect(
      within(cardNamed("Sportarr Archive")).queryByText("Default"),
    ).toBeNull();
    await settingsLoaded();
    await user.click(
      screen.getByRole("button", { name: "More actions for Sportarr Archive" }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Set as default" }),
    );
    await waitFor(() =>
      expect(
        within(cardNamed("Sportarr Archive")).getByText("Default"),
      ).toBeInTheDocument(),
    );
    expect(
      within(cardNamed("Main Sportarr")).queryByText("Default"),
    ).toBeNull();
    expect(
      within(cardNamed("Sportarr Archive")).getByRole("switch", {
        name: "Disable instance",
      }),
    ).toBeChecked();
    expect(screen.getAllByText("Default")).toHaveLength(1);
    expect(updates).toEqual([{ is_default: true }]);
    await expectOtherKindDefaults(user);

    server.use(
      http.patch("/api/system/arr-instances/43", async ({ request }) => {
        updates.push(await request.json());
        saved = afterDisable;
        return HttpResponse.json(disabled);
      }),
    );
    await user.click(
      within(cardNamed("Sportarr Archive")).getByRole("switch", {
        name: "Disable instance",
      }),
    );
    await waitFor(() =>
      expect(
        within(cardNamed("Sportarr Archive")).getByText("Disabled"),
      ).toBeInTheDocument(),
    );
    expect(
      within(cardNamed("Sportarr Archive")).queryByText("Default"),
    ).toBeNull();
    expect(
      within(cardNamed("Sportarr Archive")).getByRole("switch", {
        name: "Enable instance",
      }),
    ).not.toBeChecked();
    expect(
      within(cardNamed("Main Sportarr")).getByText("Default"),
    ).toBeInTheDocument();
    expect(
      within(cardNamed("Main Sportarr")).getByRole("switch", {
        name: "Disable instance",
      }),
    ).toBeChecked();
    expect(screen.getAllByText("Default")).toHaveLength(1);
    expect(updates).toEqual([{ is_default: true }, { enabled: false }]);
    await expectOtherKindDefaults(user);

    server.use(
      http.patch("/api/system/arr-instances/43", async ({ request }) => {
        updates.push(await request.json());
        saved = afterEnable;
        return HttpResponse.json(sportarrSibling);
      }),
    );
    await user.click(
      within(cardNamed("Sportarr Archive")).getByRole("switch", {
        name: "Enable instance",
      }),
    );
    await waitFor(() =>
      expect(
        within(cardNamed("Sportarr Archive")).queryByText("Disabled"),
      ).toBeNull(),
    );
    expect(
      within(cardNamed("Sportarr Archive")).queryByText("Default"),
    ).toBeNull();
    expect(
      within(cardNamed("Sportarr Archive")).getByRole("switch", {
        name: "Disable instance",
      }),
    ).toBeChecked();
    expect(
      within(cardNamed("Main Sportarr")).getByText("Default"),
    ).toBeInTheDocument();
    expect(
      within(cardNamed("Main Sportarr")).getByRole("switch", {
        name: "Disable instance",
      }),
    ).toBeChecked();
    expect(screen.getAllByText("Default")).toHaveLength(1);
    expect(updates).toEqual([
      { is_default: true },
      { enabled: false },
      { enabled: true },
    ]);
    await expectOtherKindDefaults(user);
  });

  it.each([false, true])(
    "deletes a Sportarr default owner only when confirmed: %s",
    async (confirm) => {
      const user = userEvent.setup();
      const beforeDelete = [
        sonarrDefault,
        radarrDefault,
        sportarr,
        sportarrSibling,
      ];
      const afterDelete = [
        sonarrDefault,
        radarrDefault,
        { ...sportarrSibling, is_default: true },
      ];
      let saved = beforeDelete;
      let deleted = false;
      server.use(
        http.get("/api/system/arr-instances", () => HttpResponse.json(saved)),
        http.delete("/api/system/arr-instances/42", () => {
          deleted = true;
          saved = afterDelete;
          return new HttpResponse(null, { status: 204 });
        }),
      );
      customRender(<SettingsConnectionsView />);
      const moreActions = await screen.findByRole("button", {
        name: "More actions for Main Sportarr",
      });
      await settingsLoaded();
      await user.click(moreActions);
      // Two separate races here. settingsLoaded above stops the settings query
      // re-rendering the tree and closing the menu; this waits for Mantine to
      // actually mount the dropdown, which it does lazily into a portal behind
      // a transition and which under coverage outruns findBy's 1s default.
      // Removing either one brings the intermittent failure back.
      await waitFor(() =>
        expect(moreActions).toHaveAttribute("aria-expanded", "true"),
      );
      await user.click(
        await screen.findByRole(
          "menuitem",
          { name: "Delete" },
          { timeout: 5000 },
        ),
      );
      const modal = await screen.findByRole("dialog", {
        name: "Delete instance",
      });
      const dialog = within(modal);
      expect(modal).toHaveTextContent("Main Sportarr (Sportarr)");
      expect(deleted).toBe(false);
      await user.click(
        await dialog.findByRole("button", {
          name: confirm ? "Delete instance" : "Cancel",
        }),
      );
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(deleted).toBe(confirm);
      if (confirm) {
        await waitFor(() =>
          expect(screen.queryByText("Main Sportarr")).toBeNull(),
        );
        expect(
          within(cardNamed("Sportarr Archive")).getByText("Default"),
        ).toBeInTheDocument();
        expect(
          within(cardNamed("Sportarr Archive")).getByRole("switch", {
            name: "Disable instance",
          }),
        ).toBeChecked();
      } else {
        expect(
          within(cardNamed("Main Sportarr")).getByText("Default"),
        ).toBeInTheDocument();
        expect(
          within(cardNamed("Main Sportarr")).getByRole("switch", {
            name: "Disable instance",
          }),
        ).toBeChecked();
        expect(
          within(cardNamed("Sportarr Archive")).queryByText("Default"),
        ).toBeNull();
      }
      expect(
        within(cardNamed("Sportarr Archive")).queryByText("Disabled"),
      ).toBeNull();
      expect(screen.getAllByText("Default")).toHaveLength(1);
      await expectOtherKindDefaults(user);
    },
  );

  it.each(["sonarr", "radarr"] as const)(
    "preserves webhook and apply-profile actions for %s",
    async (kind) => {
      const instance = makeInstance({
        kind,
        media_defaults: { default_enabled: true, default_profile: 3 },
      });
      customRender(
        <>
          <InstanceCard
            instance={instance}
            onEdit={vi.fn()}
            onDelete={vi.fn()}
          />
          <InstanceFormModal
            opened
            kind={kind}
            instance={instance}
            onClose={vi.fn()}
          />
        </>,
      );
      const dialog = within(
        await screen.findByRole("dialog", { name: "Edit Main Sonarr" }),
      );
      expect(
        screen.getByRole("button", { name: "Copy webhook URL", hidden: true }),
      ).toBeInTheDocument();
      expect(
        await dialog.findByRole("button", { name: "Apply to unset" }),
      ).toBeEnabled();
    },
  );
  it("saves sports controls on the owner and applies its saved league profile", async () => {
    const user = userEvent.setup();
    let body: unknown;
    let applied = false;
    let scalarWrites = 0;
    const instance = {
      ...sportarr,
      sports_settings: {
        sports_sync: 180,
        minimum_score: 80,
        excluded_sports: ["Golf"],
        search_on_sync: true,
      },
    };
    server.use(
      http.patch("/api/system/arr-instances/42", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(instance);
      }),
      http.post("/api/system/arr-instances/42/apply-default-profile", () => {
        applied = true;
        return HttpResponse.json({ updated: 2, profileId: 3 });
      }),
      http.post("/api/system/settings", () => {
        scalarWrites++;
        return HttpResponse.json({});
      }),
    );
    customRender(
      <InstanceFormModal
        opened
        kind="sportarr"
        instance={instance}
        onClose={vi.fn()}
      />,
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Edit Main Sportarr" }),
    );
    const apply = await dialog.findByRole("button", { name: "Apply to unset" });
    expect(apply).toBeEnabled();
    await user.click(apply);
    await waitFor(() => expect(applied).toBe(true));
    // An overridden key shows its own value; an unoverridden one reads
    // "Inherited" and sends nothing.
    expect(
      dialog.getByRole("combobox", { name: "Library sync interval" }),
    ).toHaveValue("3 Hours");
    expect(
      dialog.getByRole("switch", { name: "Full subtitle scan" }),
    ).not.toBeChecked();

    // Typed through rather than pasted, so every mid-word state hits the
    // on-change handler the way a real edit does.
    const score = dialog.getByRole("textbox", { name: "Minimum score" });
    expect(score).toHaveValue("80");
    await user.clear(score);
    await user.type(score, "95");

    // Turning a row's switch off drops the key entirely, so the instance goes
    // back to inheriting the global value rather than freezing a copy of it.
    await user.click(dialog.getByRole("switch", { name: "Search after sync" }));
    await user.click(dialog.getByRole("button", { name: "Save changes" }));
    await waitFor(() =>
      expect(body).toMatchObject({
        sports_settings: {
          sports_sync: 180,
          minimum_score: 95,
          excluded_sports: ["Golf"],
        },
        media_defaults: { default_enabled: true, default_profile: 3 },
      }),
    );
    expect(
      (body as { sports_settings: Record<string, unknown> }).sports_settings,
    ).not.toHaveProperty("search_on_sync");
    expect(scalarWrites).toBe(0);
  });
});

describe("Sportarr tab layout", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/#sportarr");
    server.use(
      http.get("/api/system/languages/profiles", () =>
        HttpResponse.json([sportsProfile]),
      ),
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: {
            theme: "auto",
            use_sonarr: true,
            use_radarr: true,
            use_sportarr: true,
            minimum_score_sports: 70,
            path_mappings_sports: [],
          },
          sportarr: {
            excluded_tags: [],
            excluded_sports: [],
            only_monitored: false,
            search_on_sync: true,
          },
        }),
      ),
    );
  });

  afterEach(() => {
    window.history.replaceState({}, "", "/");
  });

  it("matches the Sonarr shape: master toggle, cards, options, path mappings", async () => {
    customRender(<SettingsConnectionsView />);

    // The master toggle sits above the cards and is always visible.
    expect(await screen.findByText("Use Sportarr")).toBeInTheDocument();

    // Options and Path Mappings appear once the toggle is on.
    await waitFor(() => {
      expect(screen.getByText("Options")).toBeInTheDocument();
      expect(screen.getByText("Path Mappings")).toBeInTheDocument();
    });

    expect(
      screen.getByText("Minimum Score For Sports Events"),
    ).toBeInTheDocument();
    expect(screen.getByText("Excluded Tags")).toBeInTheDocument();
    expect(screen.getByText("Excluded Sports")).toBeInTheDocument();
    expect(screen.getByText("Download Only Monitored")).toBeInTheDocument();
    expect(screen.getByText("Search After Sync")).toBeInTheDocument();
  });

  it("hides the options behind the master toggle", async () => {
    const user = userEvent.setup();
    customRender(<SettingsConnectionsView />);

    // Check renders a Mantine Switch, so the role is switch, not checkbox.
    const toggle = await screen.findByRole("switch", { name: "Enabled" });
    await user.click(toggle);

    // CollapseBox wraps children in a Mantine Collapse, which keeps them
    // mounted and collapses the height. Presence is therefore the wrong
    // assertion; visibility is what the toggle actually changes.
    await waitFor(() => {
      expect(screen.getByText("Options")).not.toBeVisible();
      expect(screen.getByText("Path Mappings")).not.toBeVisible();
    });
    // The instance cards stay visible regardless, same as Sonarr.
    expect(screen.getByText("Add instance")).toBeVisible();
  });
});
