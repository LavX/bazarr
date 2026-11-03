/* eslint-disable camelcase */

import { FC, useEffect, useRef, useState } from "react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  readConnectionTests,
  recordConnectionTest,
} from "@/pages/Setup/connectionTests";
import { OnboardingSelectionProvider } from "@/pages/Setup/useOnboardingSelection";
import { useOnboardingSelection } from "@/pages/Setup/useOnboardingSelection";
import { StepBusyProvider, useStepBusy } from "@/pages/Setup/useStepBusy";
import { act, customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import InstanceServerForm from "./InstanceServerForm";

// Wraps the real recorder so a test can make it throw once; every other call
// still writes the record.
vi.mock("@/pages/Setup/connectionTests", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/pages/Setup/connectionTests")>();
  return {
    ...actual,
    recordConnectionTest: vi.fn(actual.recordConnectionTest),
  };
});

const onNext = vi.fn();

// The form is handed its draft, and what this covers is what the draft is
// carrying by the time it is written, so the draft comes from the real
// provider rather than a literal.
const Harness: FC<{ saving?: boolean }> = ({ saving = false }) => {
  const { drafts, addDraft, updateDraft } = useOnboardingSelection();
  const created = useRef(false);
  useEffect(() => {
    if (created.current) {
      return;
    }
    created.current = true;
    const draft = addDraft("jellyfin");
    // What a form remounted mid-save finds: a create still in the air.
    if (saving) {
      updateDraft(draft.draftId, { submitting: true });
    }
  }, [addDraft, saving, updateDraft]);
  const draft = drafts[0];
  if (!draft) {
    return null;
  }
  return <InstanceServerForm draft={draft} onNext={onNext} onBack={vi.fn()} />;
};

// The form and the provider holding its draft, with the form able to leave the
// screen while the provider stays mounted, as the wizard does once the reader
// has gone on from a step.
const LeavingHarness: FC = () => {
  const [shown, setShown] = useState(true);
  const { drafts } = useOnboardingSelection();
  return (
    <>
      <button type="button" onClick={() => setShown(false)}>
        Leave
      </button>
      {shown && <Harness />}
      <span>{drafts[0]?.instanceId ? "row saved" : "row not saved"}</span>
    </>
  );
};

// The form on a toggle, over the one draft the provider holds. The draft is
// created once, so turning the form off and back on remounts it under the same
// provider without adding a second draft.
const ToggleHarness: FC = () => {
  const [shown, setShown] = useState(true);
  const { drafts, addDraft } = useOnboardingSelection();
  const created = useRef(false);
  useEffect(() => {
    if (created.current) {
      return;
    }
    created.current = true;
    addDraft("jellyfin");
  }, [addDraft]);
  const draft = drafts[0];
  return (
    <>
      <button type="button" onClick={() => setShown((on) => !on)}>
        Toggle form
      </button>
      {shown && draft && (
        <InstanceServerForm draft={draft} onNext={onNext} onBack={vi.fn()} />
      )}
    </>
  );
};

function ShellBusy() {
  return <span>{useStepBusy() ? "step busy" : "step free"}</span>;
}

function stageBackend(probeSucceeds = true) {
  const asked: string[] = [];
  const created: Record<string, unknown>[] = [];
  server.use(
    http.post("/api/system/media-server-instances/probe", () =>
      HttpResponse.json(
        probeSucceeds
          ? { success: true, server_name: "Attic" }
          : { success: false, error_code: "unauthorized" },
      ),
    ),
    http.post(
      "/api/system/media-server-instances/probe-libraries",
      async ({ request }) => {
        const body = (await request.json()) as { url: string };
        asked.push(body.url);
        return HttpResponse.json({
          data: [{ id: "lib-a", name: "Films", type: "movies", paths: [] }],
          error_code: null,
        });
      },
    ),
    http.post("/api/system/media-server-instances", async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      created.push(body);
      return HttpResponse.json({
        id: "row-1",
        kind: "jellyfin",
        name: String(body.name),
        enabled: true,
        url: String(body.url),
        verify_ssl: true,
        api_key_set: true,
        path_mappings: [],
        refresh_movies: true,
        refresh_episodes: true,
        options: {},
      });
    }),
    http.post(
      "/api/system/settings",
      () => new HttpResponse(null, { status: 204 }),
    ),
  );
  return { asked, created };
}

describe("InstanceServerForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("drops the libraries chosen against the server that answered before", async () => {
    // A library handle belongs to the server that handed it out. Editing a
    // connection field resets the test and libraries hooks, which takes the
    // picker off screen, so the old server's choices sat in the draft with
    // nothing saying so and Continue wrote a library id from one server
    // against another.
    const { asked, created } = stageBackend();
    const user = userEvent.setup();

    customRender(
      <OnboardingSelectionProvider>
        <Harness />
      </OnboardingSelectionProvider>,
    );

    const url = await screen.findByLabelText("Server URL");
    // Sentence case, as every other wizard label; Settings keeps "API Key".
    expect(screen.queryByLabelText("API Key")).not.toBeInTheDocument();
    await user.type(url, "http://a.example");
    await user.type(screen.getByLabelText("API key"), "key-a");

    await user.click(screen.getByRole("button", { name: "Test" }));
    await screen.findByText(/Connected to Attic/);

    await user.click(screen.getByRole("button", { name: "Load libraries" }));
    // Mantine gives the picker a search input and a hidden one, both labelled,
    // and every section keeps its own list mounted, so the option is taken
    // from the list this field controls.
    const fields = await screen.findAllByLabelText("Movie libraries");
    const movies = fields.find(
      (field) => field.getAttribute("type") !== "hidden",
    ) as HTMLInputElement;
    await user.click(movies);
    // A click focuses the field without opening it in jsdom, and the dropdown
    // stays out of the accessibility tree there because its transition never
    // settles. Same treatment as the Discover select helpers.
    if (movies.getAttribute("aria-expanded") !== "true") {
      await user.keyboard("{ArrowDown}");
    }
    // Sections render movies, series and sports in that order, and a sports
    // root can live in a library of any type, so the sports list offers this
    // one too. The first is the movie section's.
    await user.click(
      screen.getAllByRole("option", { name: "Films", hidden: true })[0],
    );
    // Three pickers, and the one that has been answered stops offering the
    // empty placeholder.
    await waitFor(() =>
      expect(screen.getAllByPlaceholderText("None selected")).toHaveLength(2),
    );

    // The reader points the draft at a different server.
    await user.type(screen.getByLabelText("Server URL"), "2");
    expect(screen.queryAllByLabelText("Movie libraries")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Load libraries" }));
    await waitFor(() =>
      expect(screen.getAllByPlaceholderText("None selected")).toHaveLength(3),
    );
    expect(asked).toEqual(["http://a.example", "http://a.example2"]);

    await user.click(screen.getByRole("button", { name: /connect jellyfin/i }));

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0].url).toBe("http://a.example2");
    expect(created[0].options).toEqual({});
  });

  // Saving does not wait for a Test, so Finish has to be told whether one
  // passed. It used to call every saved server connected, a wrong key too.
  async function connect(test: boolean) {
    const user = userEvent.setup();
    customRender(
      <OnboardingSelectionProvider>
        <Harness />
      </OnboardingSelectionProvider>,
    );
    await user.type(
      await screen.findByLabelText("Server URL"),
      "http://a.example",
    );
    await user.type(screen.getByLabelText("API key"), "key-a");
    if (test) {
      await user.click(screen.getByRole("button", { name: "Test" }));
      await screen.findByText(/Connected to Attic|Connection test failed/);
    }
    await user.click(screen.getByRole("button", { name: /connect jellyfin/i }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
  }

  it("tells Finish a server saved without a Test was never tested", async () => {
    stageBackend();
    await connect(false);
    expect(readConnectionTests()).toEqual({ "media-server:row-1": "untested" });
  });

  it("tells Finish when the Test passed against the saved values", async () => {
    stageBackend();
    await connect(true);
    expect(readConnectionTests()).toEqual({ "media-server:row-1": "passed" });
  });

  it("still saves after a failed Test, and tells Finish it failed", async () => {
    const { created } = stageBackend(false);
    await connect(true);
    expect(created).toHaveLength(1);
    expect(readConnectionTests()).toEqual({ "media-server:row-1": "failed" });
  });

  it("a generated media server step reports busy while its save is pending", async () => {
    // Browser Back is refused only while the step reports busy, so the flag
    // has to stay up until the save request settles.
    stageBackend();
    let settleSave!: () => void;
    const held = new Promise<void>((resolve) => {
      settleSave = resolve;
    });
    server.use(
      http.post("/api/system/media-server-instances", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        await held;
        return HttpResponse.json({
          id: "row-1",
          kind: "jellyfin",
          name: String(body.name),
          enabled: true,
          url: String(body.url),
          verify_ssl: true,
          api_key_set: true,
          path_mappings: [],
          refresh_movies: true,
          refresh_episodes: true,
          options: {},
        });
      }),
    );
    const user = userEvent.setup();
    customRender(
      <StepBusyProvider>
        <OnboardingSelectionProvider>
          <Harness />
        </OnboardingSelectionProvider>
        <ShellBusy />
      </StepBusyProvider>,
    );

    await user.type(
      await screen.findByLabelText("Server URL"),
      "http://a.example",
    );
    await user.type(screen.getByLabelText("API key"), "key-a");
    expect(screen.getByText("step free")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /connect jellyfin/i }));
    expect(await screen.findByText("step busy")).toBeInTheDocument();

    await act(async () => settleSave());
    expect(await screen.findByText("step free")).toBeInTheDocument();
  });

  it("a form remounted while its save is still in the air reports busy", async () => {
    // A remounted form has no request of its own in flight. The draft still
    // carries the create the earlier mount sent, and that keeps the step busy.
    customRender(
      <StepBusyProvider>
        <OnboardingSelectionProvider>
          <Harness saving />
        </OnboardingSelectionProvider>
        <ShellBusy />
      </StepBusyProvider>,
    );

    expect(await screen.findByText("step busy")).toBeInTheDocument();
  });

  it("a remount during a pending switch retry still reports the step busy", async () => {
    // The retry holds the step busy on the draft, not in the form, so a form
    // put back on screen while the retry is in the air still reports busy.
    // The first switch write is refused. The retry is held open, then refused
    // as well on release, so the warning stays and nothing advances.
    stageBackend();
    let settleRetry!: () => void;
    const held = new Promise<void>((resolve) => {
      settleRetry = resolve;
    });
    let switchWrites = 0;
    server.use(
      http.post("/api/system/settings", async () => {
        switchWrites += 1;
        if (switchWrites === 1) {
          return new HttpResponse(null, { status: 500 });
        }
        await held;
        return new HttpResponse(null, { status: 500 });
      }),
    );
    const user = userEvent.setup();
    customRender(
      <StepBusyProvider>
        <OnboardingSelectionProvider>
          <ToggleHarness />
        </OnboardingSelectionProvider>
        <ShellBusy />
      </StepBusyProvider>,
    );

    await user.type(
      await screen.findByLabelText("Server URL"),
      "http://a.example",
    );
    await user.type(screen.getByLabelText("API key"), "key-a");
    await user.click(screen.getByRole("button", { name: /connect jellyfin/i }));
    expect(
      await screen.findByText(/is saved but switched off/i),
    ).toBeInTheDocument();
    expect(await screen.findByText("step free")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /turn on jellyfin and continue/i }),
    );
    expect(await screen.findByText("step busy")).toBeInTheDocument();

    // Off and back on: the form remounts under the same provider.
    await user.click(screen.getByRole("button", { name: "Toggle form" }));
    expect(screen.queryByLabelText("Server URL")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Toggle form" }));
    expect(await screen.findByText("step busy")).toBeInTheDocument();

    await act(async () => settleRetry());
    expect(await screen.findByText("step free")).toBeInTheDocument();
    expect(screen.getByText(/is saved but switched off/i)).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
  });

  it("a failed switch retry shows the backend reason", async () => {
    // The switch write is refused with a reason of its own. The retry shows
    // that reason in the failure alert, and the step stays where it is.
    stageBackend();
    server.use(
      http.post("/api/system/settings", () =>
        HttpResponse.json(
          { message: "Settings are read-only on this server." },
          { status: 500 },
        ),
      ),
    );
    const user = userEvent.setup();
    customRender(
      <OnboardingSelectionProvider>
        <Harness />
      </OnboardingSelectionProvider>,
    );

    await user.type(
      await screen.findByLabelText("Server URL"),
      "http://a.example",
    );
    await user.type(screen.getByLabelText("API key"), "key-a");
    await user.click(screen.getByRole("button", { name: /connect jellyfin/i }));
    expect(
      await screen.findByText(/is saved but switched off/i),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /turn on jellyfin and continue/i }),
    );

    expect(
      screen.queryByText(/Settings are read-only on this server/),
    ).not.toBeInTheDocument();
    expect(
      await screen.findByText(/Settings are read-only on this server/),
    ).toBeInTheDocument();
    expect(screen.getByText(/is saved but switched off/i)).toBeInTheDocument();
    expect(screen.getByText(/could not turn on jellyfin/i)).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
  });

  it("a save whose connection-test record throws after its row landed reports the switch as off and frees the step", async () => {
    // The row lands before its Test is recorded, so this failure leaves a row
    // on the server whose master switch was never written.
    stageBackend();
    vi.mocked(recordConnectionTest).mockImplementationOnce(() => {
      throw new Error("Connection test record failed.");
    });
    const user = userEvent.setup();
    customRender(
      <StepBusyProvider>
        <OnboardingSelectionProvider>
          <Harness />
        </OnboardingSelectionProvider>
        <ShellBusy />
      </StepBusyProvider>,
    );

    await user.type(
      await screen.findByLabelText("Server URL"),
      "http://a.example",
    );
    await user.type(screen.getByLabelText("API key"), "key-a");
    await user.click(screen.getByRole("button", { name: /connect jellyfin/i }));

    expect(
      await screen.findByText(/is saved but switched off/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/could not save/i)).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /turn on jellyfin and continue/i }),
    ).toBeInTheDocument();
    expect(await screen.findByText("step free")).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
  });

  it("a connection test and a library load in flight leave the step free", async () => {
    // Browser Back is refused only while the step reports busy. Testing a
    // connection and listing libraries are reads, so neither one holds it.
    stageBackend();
    let settleTest!: () => void;
    const heldTest = new Promise<void>((resolve) => {
      settleTest = resolve;
    });
    let settleList!: () => void;
    const heldList = new Promise<void>((resolve) => {
      settleList = resolve;
    });
    const probing = vi.fn();
    const listing = vi.fn();
    server.use(
      http.post("/api/system/media-server-instances/probe", async () => {
        probing();
        await heldTest;
        return HttpResponse.json({ success: true, server_name: "Attic" });
      }),
      http.post(
        "/api/system/media-server-instances/probe-libraries",
        async () => {
          listing();
          await heldList;
          return HttpResponse.json({
            data: [{ id: "lib-a", name: "Films", type: "movies", paths: [] }],
            error_code: null,
          });
        },
      ),
    );
    const user = userEvent.setup();
    customRender(
      <StepBusyProvider>
        <OnboardingSelectionProvider>
          <Harness />
        </OnboardingSelectionProvider>
        <ShellBusy />
      </StepBusyProvider>,
    );

    await user.type(
      await screen.findByLabelText("Server URL"),
      "http://a.example",
    );
    await user.type(screen.getByLabelText("API key"), "key-a");
    await user.click(screen.getByRole("button", { name: "Test" }));
    await user.click(screen.getByRole("button", { name: "Load libraries" }));
    await waitFor(() => expect(probing).toHaveBeenCalled());
    await waitFor(() => expect(listing).toHaveBeenCalled());
    expect(screen.getByText("step free")).toBeInTheDocument();

    await act(async () => {
      settleTest();
      settleList();
    });
    expect(await screen.findByText(/connected to attic/i)).toBeInTheDocument();
    expect(await screen.findByText("step free")).toBeInTheDocument();
  });

  it("a save that lands after its form left the screen does not advance", async () => {
    // Leaving keeps the draft, so the save still records its row. The advance
    // is what the form withholds once it is off the screen.
    stageBackend();
    let settleSave!: () => void;
    const held = new Promise<void>((resolve) => {
      settleSave = resolve;
    });
    const started = vi.fn();
    server.use(
      http.post("/api/system/media-server-instances", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        started();
        await held;
        return HttpResponse.json({
          id: "row-1",
          kind: "jellyfin",
          name: String(body.name),
          enabled: true,
          url: String(body.url),
          verify_ssl: true,
          api_key_set: true,
          path_mappings: [],
          refresh_movies: true,
          refresh_episodes: true,
          options: {},
        });
      }),
    );
    const user = userEvent.setup();
    customRender(
      <OnboardingSelectionProvider>
        <LeavingHarness />
      </OnboardingSelectionProvider>,
    );

    await user.type(
      await screen.findByLabelText("Server URL"),
      "http://a.example",
    );
    await user.type(screen.getByLabelText("API key"), "key-a");
    await user.click(screen.getByRole("button", { name: /connect jellyfin/i }));
    await waitFor(() => expect(started).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "Leave" }));
    expect(screen.queryByLabelText("Server URL")).not.toBeInTheDocument();

    await act(async () => settleSave());
    expect(await screen.findByText("row saved")).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
  });

  it("a remounted form that is still saving does not write the server again", async () => {
    // The draft still carries the create the earlier mount sent, so Continue
    // is held and the server is not written a second time.
    const { created } = stageBackend();
    const user = userEvent.setup();
    customRender(
      <OnboardingSelectionProvider>
        <Harness saving />
      </OnboardingSelectionProvider>,
    );

    await user.type(
      await screen.findByLabelText("Server URL"),
      "http://a.example",
    );
    await user.type(screen.getByLabelText("API key"), "key-a");
    const connect = screen.getByRole("button", { name: /connect jellyfin/i });
    expect(connect).toBeDisabled();
    await user.click(connect);

    expect(created).toHaveLength(0);
    expect(onNext).not.toHaveBeenCalled();
  });
});
