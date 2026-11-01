import { StrictMode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import OnboardingWizardView from "./OnboardingWizard";

// The real router here, deliberately: what is being tested is that the wizard
// has history of its own, which a mocked useNavigate cannot show.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useSettingsMutation: vi.fn(),
  };
});

const mockedSettingsMutation = vi.mocked(useSettingsMutation);

function openWizardAt(path: string, entries: string[] = [path]) {
  const router = createMemoryRouter(
    [
      { path: "/setup", element: <OnboardingWizardView /> },
      { path: "/setup/:stepKey", element: <OnboardingWizardView /> },
      { path: "/", element: <div>home</div> },
    ],
    { initialEntries: entries, initialIndex: entries.length - 1 },
  );
  rawRender(
    <StrictMode>
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>
    </StrictMode>,
  );
  return router;
}

describe("the wizard's own URL", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mockedSettingsMutation.mockReturnValue({
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("puts the step in the URL", async () => {
    const router = openWizardAt("/setup");

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
  });

  it("opens the step the URL names, over the persisted cursor", async () => {
    localStorage.setItem("bazarr.onboarding.step", "welcome");

    openWizardAt("/setup/languages");

    expect(
      await screen.findByRole("heading", { name: /subtitle languages/i }),
    ).toBeInTheDocument();
  });

  it("browser Back moves one step instead of leaving the application", async () => {
    // Every step rendered at /setup and the Redirector arrived with replace,
    // so there was no history entry behind the wizard at all: pressing Back
    // left the application.
    const user = userEvent.setup();
    const router = openWizardAt("/setup");

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    await user.click(screen.getByRole("button", { name: /get started/i }));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/intent"),
    );

    await router.navigate(-1);

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    expect(
      await screen.findByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
  });

  it("the wizard's own Back does not turn browser Back into forward", async () => {
    // Pushing the earlier step on top of the later one made the next browser
    // Back walk forward through the wizard.
    const user = userEvent.setup();
    const router = openWizardAt("/setup");

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    await user.click(screen.getByRole("button", { name: /get started/i }));
    await screen.findByRole("heading", { name: /what do you want bazarr/i });

    await user.click(screen.getByRole("button", { name: /^back$/i }));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );

    await router.navigate(-1);

    // Out of the wizard the way it was entered, not forward into it again.
    await waitFor(() =>
      expect(router.state.location.pathname).not.toBe("/setup/intent"),
    );
  });

  it("Back walks the history that is there instead of writing more", async () => {
    // Replacing the current entry left the same step in the two entries on
    // top, so the first browser Back after a wizard Back appeared to do
    // nothing and a second press was needed to get anywhere.
    const user = userEvent.setup();
    const router = openWizardAt("/setup", ["/", "/setup"]);

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    await user.click(screen.getByRole("button", { name: /get started/i }));
    await screen.findByRole("heading", { name: /what do you want bazarr/i });

    await user.click(screen.getByRole("button", { name: /^back$/i }));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    expect(
      await screen.findByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();

    // One press, one move: out of the wizard the way it was entered.
    await router.navigate(-1);

    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });

  it("Back still walks the history after a browser Forward", async () => {
    const user = userEvent.setup();
    const router = openWizardAt("/setup", ["/", "/setup"]);

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    await user.click(screen.getByRole("button", { name: /get started/i }));
    await screen.findByRole("heading", { name: /what do you want bazarr/i });

    await act(async () => {
      await router.navigate(-1);
    });
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    await act(async () => {
      await router.navigate(1);
    });
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/intent"),
    );

    await user.click(await screen.findByRole("button", { name: /^back$/i }));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );

    // Still one press, one move: the entry the Forward restored was not
    // written over.
    await router.navigate(-1);

    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });

  it("does not read the stored cursor as an entry to go back to", async () => {
    // A link opened straight onto a step resolves against the stored cursor,
    // and that step was never an entry in this browser's history. Counting it
    // as one made the wizard's Back leave the application.
    localStorage.setItem("bazarr.onboarding.step", "welcome");
    const user = userEvent.setup();
    const router = openWizardAt("/setup/intent", ["/", "/setup/intent"]);

    await screen.findByRole("heading", { name: /what do you want bazarr/i });

    await user.click(screen.getByRole("button", { name: /^back$/i }));

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    expect(
      screen.getByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
  });

  it("falls back to the persisted cursor when the URL names no step", async () => {
    localStorage.setItem("bazarr.onboarding.step", "languages");

    const router = openWizardAt("/setup");

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/languages"),
    );
  });

  it("a step the run does not walk lands on a real step", async () => {
    const router = openWizardAt("/setup/not-a-step");

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    expect(
      screen.getByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
  });

  it("a save that lands after the step was left does not move the wizard", async () => {
    // The translator step reports itself busy from settings.isPending, which
    // this test's mock does not supply. Here the step reads as free, so browser
    // Back can unmount it with its write still in the air. The callback the
    // step handed to that write still runs when the write answers, and the
    // wizard ignores it because the step is no longer on screen.
    let landed: (() => void) | undefined;
    mockedSettingsMutation.mockReturnValue({
      mutate: vi.fn(
        (_payload: unknown, options?: { onSuccess?: () => void }) => {
          landed = options?.onSuccess;
        },
      ),
    } as unknown as ReturnType<typeof useSettingsMutation>);
    const user = userEvent.setup();
    const router = openWizardAt("/setup/translator", [
      "/setup/welcome",
      "/setup/translator",
    ]);

    await user.type(
      await screen.findByLabelText(/openrouter api key/i),
      "sk-1",
    );
    await user.click(screen.getByRole("button", { name: /^continue$/i }));
    expect(landed).toBeDefined();

    await act(async () => {
      await router.navigate(-1);
    });
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );

    act(() => landed?.());

    // Still where the reader went, not carried on to the step after Translation.
    expect(router.state.location.pathname).toBe("/setup/welcome");
    expect(
      screen.getByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
  });

  it("a save from an earlier visit to the same step does not move the wizard", async () => {
    // The guard used to be keyed by the step, so Back and then Forward before
    // the first save answered put the same key on screen again, as a fresh
    // mount, and the old save advanced it over the reader's second visit.
    let landed: (() => void) | undefined;
    mockedSettingsMutation.mockReturnValue({
      mutate: vi.fn(
        (_payload: unknown, options?: { onSuccess?: () => void }) => {
          landed = options?.onSuccess;
        },
      ),
    } as unknown as ReturnType<typeof useSettingsMutation>);
    const user = userEvent.setup();
    const router = openWizardAt("/setup/translator", [
      "/setup/welcome",
      "/setup/translator",
    ]);

    await user.type(
      await screen.findByLabelText(/openrouter api key/i),
      "sk-1",
    );
    await user.click(screen.getByRole("button", { name: /^continue$/i }));
    expect(landed).toBeDefined();
    const firstVisit = landed;

    await act(async () => {
      await router.navigate(-1);
    });
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    await act(async () => {
      await router.navigate(1);
    });
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/translator"),
    );
    expect(await screen.findByLabelText(/openrouter api key/i)).toBeVisible();

    act(() => firstVisit?.());

    // Still on the second visit, not carried on to the step after Translation.
    expect(router.state.location.pathname).toBe("/setup/translator");
    expect(screen.getByLabelText(/openrouter api key/i)).toBeVisible();
  });

  it("a save that lands while the step is still on screen still advances", async () => {
    let landed: (() => void) | undefined;
    mockedSettingsMutation.mockReturnValue({
      mutate: vi.fn(
        (_payload: unknown, options?: { onSuccess?: () => void }) => {
          landed = options?.onSuccess;
        },
      ),
    } as unknown as ReturnType<typeof useSettingsMutation>);
    const user = userEvent.setup();
    const router = openWizardAt("/setup/translator");

    await user.type(
      await screen.findByLabelText(/openrouter api key/i),
      "sk-1",
    );
    await user.click(screen.getByRole("button", { name: /^continue$/i }));
    act(() => landed?.());

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/general"),
    );
  });

  it("browser Back stays on a media server step while it saves", async () => {
    // The Jellyfin configure step is where a media server gets saved. Its
    // create request is held open, so the step is busy because a real save is
    // still running, not because a mocked value says so.
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const requested = vi.fn();
    server.use(
      http.post("/api/system/media-server-instances", async () => {
        requested();
        await held;
        return new HttpResponse(null, { status: 400 });
      }),
    );
    const draft = {
      draftId: "jellyfin-draft",
      kind: "jellyfin",
      name: "Jellyfin",
      url: "",
      verifySsl: true,
      pathMappings: [],
      options: {},
    };
    localStorage.setItem(
      "bazarr.onboarding.media-servers",
      JSON.stringify([draft]),
    );
    const stepPath = "/setup/media-server-jellyfin-draft";
    const user = userEvent.setup();
    const router = openWizardAt(stepPath, ["/setup/welcome", stepPath]);

    await user.type(
      await screen.findByLabelText(/server url/i),
      "http://10.0.0.9:8096",
    );
    await user.type(screen.getByLabelText(/api key/i), "jelly-key");
    await user.click(screen.getByRole("button", { name: /connect jellyfin/i }));
    await waitFor(() => expect(requested).toHaveBeenCalledTimes(1));
    // The skip is only offered while the step is idle, so it going away shows
    // the wizard has taken the busy state in.
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /do this later/i }),
      ).not.toBeInTheDocument(),
    );

    // Browser Back is refused while the save runs, so the wizard stays put.
    await act(async () => {
      await router.navigate(-1);
    });

    expect(router.state.location.pathname).toBe(stepPath);
    expect(
      screen.getByRole("heading", { name: /^jellyfin$/i }),
    ).toBeInTheDocument();

    // Refusing the create settles the save, so nothing is left in the air.
    release?.();
    await screen.findByText(/could not save jellyfin/i);
  });

  it("browser Back leaves a media server step once it is idle", async () => {
    // Nothing is being saved, so the same browser Back moves the wizard off
    // the configure step.
    const draft = {
      draftId: "jellyfin-draft",
      kind: "jellyfin",
      name: "Jellyfin",
      url: "",
      verifySsl: true,
      pathMappings: [],
      options: {},
    };
    localStorage.setItem(
      "bazarr.onboarding.media-servers",
      JSON.stringify([draft]),
    );
    const stepPath = "/setup/media-server-jellyfin-draft";
    const router = openWizardAt(stepPath, ["/setup/welcome", stepPath]);

    await screen.findByRole("heading", { name: /^jellyfin$/i });
    await act(async () => {
      await router.navigate(-1);
    });

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    expect(
      await screen.findByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
  });
});
