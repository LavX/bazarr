import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MediaServerKind } from "@/apis/raw/mediaServers";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import OnboardingWizardView from "./OnboardingWizard";

const navigate = vi.fn();

vi.mock("react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router")>();
  return {
    ...actual,
    useNavigate: () => navigate,
  };
});

interface Row {
  id: string;
  kind: MediaServerKind;
  name: string;
}

let rows: Row[] = [];
let creates: unknown[] = [];
let settingsWrites: unknown[] = [];
let refuse: MediaServerKind | null = null;
// The rows land but the write that turns their master switch on does not.
let refuseSwitch = false;

function serialize(row: Row) {
  return {
    ...row,
    enabled: true,
    url: "http://10.0.0.9:8096",
    verify_ssl: true,
    api_key_set: true,
    path_mappings: [],
    refresh_movies: true,
    refresh_episodes: true,
    options: {},
  };
}

function stageBackend() {
  server.use(
    http.get("/api/system/media-server-instances", ({ request }) => {
      const kind = new URL(request.url).searchParams.get("kind");
      return HttpResponse.json({
        data: rows.filter((row) => row.kind === kind).map(serialize),
      });
    }),
    http.post("/api/system/media-server-instances", async ({ request }) => {
      const body = (await request.json()) as {
        kind: MediaServerKind;
        name: string;
      };
      creates.push(body);
      if (refuse === body.kind) {
        return HttpResponse.json(
          { message: "Refused by the server." },
          {
            status: 400,
          },
        );
      }
      const row = {
        id: `row-${rows.length + 1}`,
        kind: body.kind,
        name: body.name,
      };
      rows.push(row);
      return HttpResponse.json(serialize(row));
    }),
    http.delete("/api/system/media-server-instances/:id", ({ params }) => {
      rows = rows.filter((row) => row.id !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
    // The settings writer posts FormData, not JSON.
    http.post("/api/system/settings", async ({ request }) => {
      const written = Object.fromEntries((await request.formData()).entries());
      if (
        refuseSwitch &&
        Object.keys(written).some((key) =>
          key.startsWith("settings-general-use_"),
        )
      ) {
        return new HttpResponse(null, { status: 500 });
      }
      settingsWrites.push(written);
      return new HttpResponse(null, { status: 204 });
    }),
    http.get("/api/system/settings", () =>
      HttpResponse.json({ data: { general: {}, translator: {} } }),
    ),
  );
}

/** Land the shell on the media server picker, library path. */
function startOnPicker() {
  localStorage.setItem("bazarr.onboarding.intent", "library");
  localStorage.setItem("bazarr.onboarding.step", "media-servers");
}

describe("media server selection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // clearAllMocks keeps implementations, and one test below gives navigate
    // one of its own.
    navigate.mockReset();
    localStorage.clear();
    rows = [];
    creates = [];
    settingsWrites = [];
    refuse = null;
    refuseSwitch = false;
    stageBackend();
    startOnPicker();
  });

  afterEach(() => localStorage.clear());

  it("gives every ticked server a configure step of its own", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await screen.findByRole("heading", { name: /^media servers$/i });
    await user.click(screen.getByRole("checkbox", { name: /^Jellyfin$/ }));
    await user.click(screen.getByRole("checkbox", { name: /^Emby$/ }));
    await user.click(screen.getByRole("button", { name: /set up 2 servers/i }));

    // One server per screen, in the order the picker lists them.
    expect(
      await screen.findByRole("heading", { name: /^jellyfin$/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/media servers 2 of 3/i)).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /continue without jellyfin/i }),
    );
    expect(
      await screen.findByRole("heading", { name: /^emby$/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/media servers 3 of 3/i)).toBeInTheDocument();
  });

  it("Back into the picker, retick, and forward lands on the right screen", async () => {
    // The cursor is a key for exactly this: the reader reaches the picker by
    // pressing Back out of the segment it generates, changes what is ticked,
    // and walks forward into a renumbered list.
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await screen.findByRole("heading", { name: /^media servers$/i });
    await user.click(screen.getByRole("checkbox", { name: /^Jellyfin$/ }));
    await user.click(screen.getByRole("checkbox", { name: /^Emby$/ }));
    await user.click(screen.getByRole("button", { name: /set up 2 servers/i }));
    await screen.findByRole("heading", { name: /^jellyfin$/i });

    await user.click(screen.getByRole("button", { name: /^back$/i }));
    await screen.findByRole("heading", { name: /^media servers$/i });

    // Untick Jellyfin. Nothing was written, so the step simply goes.
    await user.click(screen.getByRole("checkbox", { name: /^Jellyfin$/ }));
    await user.click(screen.getByRole("button", { name: /set up 1 server/i }));

    expect(
      await screen.findByRole("heading", { name: /^emby$/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: /^jellyfin$/i }),
    ).not.toBeInTheDocument();
  });

  it("unticking the step you are standing on lands you on the picker", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await screen.findByRole("heading", { name: /^media servers$/i });
    await user.click(screen.getByRole("checkbox", { name: /^Silo$/ }));
    await user.click(screen.getByRole("button", { name: /set up 1 server/i }));
    await screen.findByRole("heading", { name: /^silo$/i });

    await user.click(screen.getByRole("button", { name: /^back$/i }));
    await user.click(screen.getByRole("checkbox", { name: /^Silo$/ }));

    // Never a blank screen: the nearest surviving earlier step is the picker.
    expect(
      screen.getByRole("heading", { name: /^media servers$/i }),
    ).toBeInTheDocument();
  });

  it("saves a server, flips one switch, and shows it connected on the picker", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await screen.findByRole("heading", { name: /^media servers$/i });
    await user.click(screen.getByRole("checkbox", { name: /^Jellyfin$/ }));
    await user.click(screen.getByRole("button", { name: /set up 1 server/i }));
    await screen.findByRole("heading", { name: /^jellyfin$/i });

    await user.type(
      screen.getByLabelText(/server url/i),
      "http://10.0.0.9:8096",
    );
    await user.type(screen.getByLabelText(/api key/i), "jelly-key");
    await user.click(screen.getByRole("button", { name: /connect jellyfin/i }));

    await waitFor(() => expect(creates).toHaveLength(1));
    // One write, carrying only the kinds that actually landed.
    await waitFor(() =>
      expect(settingsWrites).toEqual([
        { "settings-general-use_jellyfin": "true" },
      ]),
    );

    // The configure step is gone, so Continue moved on to Seerr.
    expect(
      await screen.findByRole("heading", { name: /^seerr$/i }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^back$/i }));
    await screen.findByRole("heading", { name: /^media servers$/i });
    expect(await screen.findByText(/^connected$/i)).toBeInTheDocument();
    const checkbox = screen.getByRole("checkbox", { name: /^Jellyfin$/ });
    await waitFor(() => expect(checkbox).toBeChecked());
    expect(checkbox).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /^disconnect$/i }),
    ).toBeInTheDocument();
  });

  it("names a refused save instead of advancing as if it landed", async () => {
    refuse = "emby";
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await screen.findByRole("heading", { name: /^media servers$/i });
    await user.click(screen.getByRole("checkbox", { name: /^Emby$/ }));
    await user.click(screen.getByRole("button", { name: /set up 1 server/i }));
    await screen.findByRole("heading", { name: /^emby$/i });

    await user.type(
      screen.getByLabelText(/server url/i),
      "http://10.0.0.9:8096",
    );
    await user.type(screen.getByLabelText(/api key/i), "emby-key");
    await user.type(screen.getByLabelText(/local path 1/i), "/tv");
    await user.type(screen.getByLabelText(/server path 1/i), "/media/tv");
    await user.click(screen.getByRole("button", { name: /connect emby/i }));

    expect(await screen.findByText(/could not save emby/i)).toBeInTheDocument();
    // No master switch was flipped for a row that never landed.
    expect(settingsWrites).toEqual([]);
    expect(
      screen.getByRole("button", { name: /try emby again/i }),
    ).toBeInTheDocument();
  });

  async function connectJellyfin(user: ReturnType<typeof userEvent.setup>) {
    await screen.findByRole("heading", { name: /^media servers$/i });
    await user.click(screen.getByRole("checkbox", { name: /^Jellyfin$/ }));
    await user.click(screen.getByRole("button", { name: /set up 1 server/i }));
    await screen.findByRole("heading", { name: /^jellyfin$/i });
    await user.type(
      screen.getByLabelText(/server url/i),
      "http://10.0.0.9:8096",
    );
    await user.type(screen.getByLabelText(/api key/i), "jelly-key");
    await user.click(screen.getByRole("button", { name: /connect jellyfin/i }));
  }

  it("keeps a failed master switch on screen instead of walking past it", async () => {
    // Marking the draft saved is what removes this step from the wizard, so
    // setting the warning and advancing in the same breath unmounted it before
    // it could render: the reader reached Finish with the server reported as
    // connected and nothing refreshing.
    refuseSwitch = true;
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await connectJellyfin(user);

    expect(
      await screen.findByText(/master switch could not be turned on/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /^jellyfin$/i }),
    ).toBeInTheDocument();
    expect(creates).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: /continue anyway/i }));

    expect(
      await screen.findByRole("heading", { name: /^seerr$/i }),
    ).toBeInTheDocument();
    // The row was already written; accepting the warning must not write it
    // again.
    expect(creates).toHaveLength(1);
  });

  it("still says so after a trip back to the picker", async () => {
    refuseSwitch = true;
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await connectJellyfin(user);
    await screen.findByText(/master switch could not be turned on/i);

    await user.click(screen.getByRole("button", { name: /^back$/i }));
    await screen.findByRole("heading", { name: /^media servers$/i });
    await user.click(screen.getByRole("button", { name: /set up 1 server/i }));

    // Remounted, and the warning is still the state of that server.
    expect(
      await screen.findByText(/master switch could not be turned on/i),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /continue anyway/i }));

    await screen.findByRole("heading", { name: /^seerr$/i });
    expect(creates).toHaveLength(1);
  });

  it("Skip setup forgets the servers that were ticked", async () => {
    // clearSelection only queues a state update, and the persistence effect
    // runs after the commit; the navigation unmounts the provider in that same
    // commit, so the drafts could still be in localStorage to be restored on
    // the next visit to setup.
    let storedOnLeaving: string | null = "not read";
    navigate.mockImplementation(() => {
      storedOnLeaving = localStorage.getItem("bazarr.onboarding.media-servers");
    });
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await screen.findByRole("heading", { name: /^media servers$/i });
    await user.click(screen.getByRole("checkbox", { name: /^Jellyfin$/ }));
    await waitFor(() =>
      expect(
        localStorage.getItem("bazarr.onboarding.media-servers"),
      ).not.toBeNull(),
    );

    await user.click(screen.getByRole("button", { name: /skip setup/i }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/"));
    expect(storedOnLeaving).toBeNull();
  });

  it("puts a validation message on the field that is wrong", async () => {
    // One alert titled "Could not connect Emby" carrying "Name is required",
    // for a connection nobody attempted, is not a field error.
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await screen.findByRole("heading", { name: /^media servers$/i });
    await user.click(screen.getByRole("checkbox", { name: /^Emby$/ }));
    await user.click(screen.getByRole("button", { name: /set up 1 server/i }));
    await screen.findByRole("heading", { name: /^emby$/i });

    await user.type(screen.getByLabelText(/server url/i), "not-a-url");
    await user.click(screen.getByRole("button", { name: /connect emby/i }));

    const url = await screen.findByLabelText(/server url/i);
    await waitFor(() => expect(url).toHaveAttribute("aria-invalid", "true"));
    expect(creates).toHaveLength(0);

    // The message goes as the reader answers it, not when Test is pressed.
    await user.type(url, "x");
    await waitFor(() =>
      expect(url).not.toHaveAttribute("aria-invalid", "true"),
    );
  });

  it("seeds the path mapping row the save cannot do without", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await screen.findByRole("heading", { name: /^media servers$/i });
    await user.click(screen.getByRole("checkbox", { name: /^Emby$/ }));
    await user.click(screen.getByRole("button", { name: /set up 1 server/i }));
    await screen.findByRole("heading", { name: /^emby$/i });

    expect(screen.getByLabelText(/local path 1/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/server path 1/i)).toBeInTheDocument();
    expect(screen.getByText(/path mappings \(required\)/i)).toBeInTheDocument();
  });
});
