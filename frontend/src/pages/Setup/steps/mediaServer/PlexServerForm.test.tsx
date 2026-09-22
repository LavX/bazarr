/* eslint-disable camelcase */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createDraft } from "@/pages/Setup/useOnboardingSelection";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import PlexServerForm from "./PlexServerForm";

// The account panel belongs to the Connections page and is covered there. What
// this step decides is what Continue does with the row that panel leaves
// behind, so the panel itself is stubbed out.
vi.mock("@/pages/Settings/Plex/PlexSettings", () => ({
  default: () => <div>plex account panel</div>,
}));

const markSaved = vi.fn();

vi.mock("@/pages/Setup/useOnboardingSelection", async (importOriginal) => {
  const actual =
    await importOriginal<
      typeof import("@/pages/Setup/useOnboardingSelection")
    >();
  return {
    ...actual,
    // markSaved is the only member this step reads; the provider itself is
    // exercised by the flow tests.
    useOnboardingSelection: () => ({ markSaved }),
  };
});

const onNext = vi.fn();

function plexRow(id: string) {
  return {
    id,
    kind: "plex",
    name: "Plex",
    enabled: true,
    url: "http://10.0.0.9:32400",
    verify_ssl: true,
    api_key_set: true,
    path_mappings: [],
    refresh_movies: true,
    refresh_episodes: true,
    options: {},
  };
}

/** The list the account flow writes to, with the calls it received. */
function stageInstances(rows: () => unknown[]) {
  const calls: string[] = [];
  server.use(
    http.get("/api/system/media-server-instances", () => {
      calls.push("list");
      return HttpResponse.json({ data: rows() });
    }),
  );
  return calls;
}

/**
 * Which Plex row the account owns, as the backend records it
 * (media_servers/plex_account.py). A reader can add Plex servers by hand in
 * Settings, so being in the list says nothing about who made a row.
 */
function stageOwner(instanceId: () => string | undefined) {
  server.use(
    http.get("/api/system/settings", () => {
      const recorded = instanceId();
      return HttpResponse.json({
        general: { theme: "auto" },
        plex: recorded === undefined ? {} : { instance_id: recorded },
      });
    }),
  );
}

describe("PlexServerForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("asks for the Plex row again before deciding the draft is saved", async () => {
    // This query is mounted while no destination row exists, and selecting a
    // server invalidates the Plex account queries only, so the cached empty
    // array outlives the row being created. The draft was then never marked
    // saved and its step stayed in the wizard, sending the reader back through
    // a connection they had already made.
    let rows: unknown[] = [];
    let owner: string | undefined = undefined;
    const calls = stageInstances(() => rows);
    stageOwner(() => owner);
    const draft = createDraft("plex");
    const user = userEvent.setup();

    customRender(
      <PlexServerForm draft={draft} onNext={onNext} onBack={vi.fn()} />,
    );

    await screen.findByText("plex account panel");
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));

    // The account flow creates the row while this step is on screen, and
    // records it as the one the account owns.
    rows = [plexRow("plex-1")];
    owner = "plex-1";
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    await waitFor(() =>
      expect(markSaved).toHaveBeenCalledWith(draft.draftId, "plex-1"),
    );
    expect(onNext).toHaveBeenCalled();
  });

  it("marks the row the account owns, not the first Plex row there is", async () => {
    // A Plex server somebody added by hand in Settings can sort ahead of the
    // account's own destination. Taking the first row bound this draft to
    // that server: the wizard then reported a connection it had not made,
    // and the step for the one it had stopped being generated.
    let rows: unknown[] = [];
    let owner: string | undefined = undefined;
    const calls = stageInstances(() => rows);
    stageOwner(() => owner);
    const draft = createDraft("plex");
    const user = userEvent.setup();

    customRender(
      <PlexServerForm draft={draft} onNext={onNext} onBack={vi.fn()} />,
    );

    await screen.findByText("plex account panel");
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));

    rows = [plexRow("loft"), plexRow("account")];
    owner = "account";
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    await waitFor(() =>
      expect(markSaved).toHaveBeenCalledWith(draft.draftId, "account"),
    );
    expect(markSaved).not.toHaveBeenCalledWith(draft.draftId, "loft");
  });

  it("marks nothing while no row is recorded as the account's", async () => {
    // A row with no recorded owner is a row the account flow did not make, so
    // there is nothing this draft can honestly claim. The settings are asked
    // again on Continue for exactly this reason: the recorded id is written
    // by the same transition that makes the row.
    const calls = stageInstances(() => [plexRow("loft")]);
    stageOwner(() => undefined);
    const draft = createDraft("plex");
    const user = userEvent.setup();

    customRender(
      <PlexServerForm draft={draft} onNext={onNext} onBack={vi.fn()} />,
    );

    await screen.findByText("plex account panel");
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(markSaved).not.toHaveBeenCalled();
  });

  it("advances without marking anything when no server was chosen", async () => {
    const calls = stageInstances(() => []);
    stageOwner(() => undefined);
    const draft = createDraft("plex");
    const user = userEvent.setup();

    customRender(<PlexServerForm draft={draft} onNext={onNext} />);

    await screen.findByText("plex account panel");
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(markSaved).not.toHaveBeenCalled();
  });
});
