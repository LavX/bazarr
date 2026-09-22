import userEvent, { UserEvent } from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { customRender, screen, waitFor } from "@/tests";
import OnboardingWizardView from "./OnboardingWizard";

// Navigation + the settings mutation are the only external effects we assert.
const navigate = vi.fn();
const mutate = vi.fn();

vi.mock("react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router")>();
  return {
    ...actual,
    useNavigate: () => navigate,
  };
});

// Keep the real barrel (AllProviders' ThemeLoader reads useSystemSettings from
// it) and override only the settings mutation we assert on.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useSettingsMutation: vi.fn(),
  };
});

const mockedSettingsMutation = vi.mocked(useSettingsMutation);

describe("OnboardingWizardView", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mockedSettingsMutation.mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("renders the Welcome step at step 0", () => {
    customRender(<OnboardingWizardView />);

    expect(
      screen.getByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /get started/i }),
    ).toBeInTheDocument();
  });

  it("advances past Welcome when Get started is clicked", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await user.click(screen.getByRole("button", { name: /get started/i }));

    await waitFor(() => {
      // The cursor is a step key, not an index into a list that is generated
      // and renumbers under the reader.
      expect(localStorage.getItem("bazarr.onboarding.step")).toBe("intent");
    });
    // Welcome hands straight over to the one question the rest follows from.
    expect(
      await screen.findByRole("heading", { name: /what do you want bazarr/i }),
    ).toBeInTheDocument();
  });

  async function answerIntent(user: UserEvent, answer: RegExp) {
    await user.click(screen.getByRole("button", { name: /get started/i }));
    await user.click(await screen.findByRole("radio", { name: answer }));
    await user.click(screen.getByRole("button", { name: /^continue$/i }));
  }

  it("the library path walks the arr steps", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await answerIntent(user, /sonarr, radarr or sportarr/i);

    expect(
      await screen.findByRole("heading", { name: /^sonarr$/i }),
    ).toBeInTheDocument();
    // A phase and a position inside it, never a step total: the total is not
    // knowable before the path is answered and moves once servers are ticked.
    expect(screen.getByText(/connect \u00b7 1 of 5/i)).toBeInTheDocument();
  });

  it("the discover path asks for no arr instance at all", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await answerIntent(user, /find subtitles for anything/i);

    // Media servers, not Sonarr: connecting one has nothing to do with running
    // an arr, and this step used to be hidden from everyone who ran neither.
    expect(
      await screen.findByRole("heading", { name: /^media servers$/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/connect \u00b7 media servers 1 of 1/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: /^sonarr$/i }),
    ).not.toBeInTheDocument();
  });

  it("changing the answer re-filters the rail and keeps the answer", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await answerIntent(user, /sonarr, radarr or sportarr/i);
    await screen.findByRole("heading", { name: /^sonarr$/i });

    // Back onto the question: the previous answer is still the selected one,
    // so changing it is a choice rather than a fresh guess.
    await user.click(screen.getByRole("button", { name: /back/i }));
    const library = await screen.findByRole("radio", {
      name: /sonarr, radarr or sportarr/i,
    });
    expect(library).toBeChecked();

    await user.click(
      screen.getByRole("radio", { name: /find subtitles for anything/i }),
    );
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    expect(
      await screen.findByRole("heading", { name: /^media servers$/i }),
    ).toBeInTheDocument();
  });

  it("the shell renders the skip control for an optional step", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await answerIntent(user, /sonarr, radarr or sportarr/i);
    await screen.findByRole("heading", { name: /^sonarr$/i });

    await user.click(screen.getByRole("button", { name: /do this later/i }));

    expect(
      await screen.findByRole("heading", { name: /^radarr$/i }),
    ).toBeInTheDocument();
  });

  it("a step that cannot be skipped says why instead", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await user.click(screen.getByRole("button", { name: /get started/i }));
    await screen.findByRole("heading", { name: /what do you want bazarr/i });

    expect(
      screen.queryByRole("button", { name: /do this later/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/nothing here is locked in/i)).toBeInTheDocument();
  });

  it("Finish does not re-filter the rail out from under the reader", async () => {
    // Clearing the intent through React state would re-filter the rail while
    // the step index still belongs to the path being left, so the reader would
    // watch the wizard jump back to a step they already finished in the moment
    // between pressing Finish and the app taking over.
    const user = userEvent.setup();
    localStorage.setItem("bazarr.onboarding.intent", "discover");
    localStorage.setItem("bazarr.onboarding.step", "finish");
    let onSuccess: (() => void) | undefined;
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        onSuccess = opts?.onSuccess;
      },
    );

    customRender(<OnboardingWizardView />);

    await screen.findByRole("heading", { name: /you are all set/i });
    await user.click(
      screen.getByRole("button", { name: /finish and open discover/i }),
    );
    onSuccess?.();

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/discover"));
    expect(
      screen.getByRole("heading", { name: /you are all set/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/finish \u00b7 2 of 2/i)).toBeInTheDocument();
  });

  it("Set up later asks before it ends onboarding", async () => {
    // One click used to end onboarding for good, with no confirmation and
    // nothing in the application linking back to /setup afterwards.
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await user.click(screen.getByRole("button", { name: /set up later/i }));

    expect(mutate).not.toHaveBeenCalled();
    // The promise the confirmation makes, which Settings, General now keeps.
    expect(await screen.findByText(/settings,\s*general/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /keep going/i }));

    expect(mutate).not.toHaveBeenCalled();
    expect(
      screen.getByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
  });

  it("confirming Set up later saves setup_complete and navigates home", async () => {
    const user = userEvent.setup();
    let onSuccess: (() => void) | undefined;
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        onSuccess = opts?.onSuccess;
      },
    );

    customRender(<OnboardingWizardView />);

    await user.click(screen.getByRole("button", { name: /set up later/i }));
    await user.click(
      await screen.findByRole("button", { name: /^leave setup$/i }),
    );

    expect(mutate).toHaveBeenCalledWith(
      { "settings-general-setup_complete": true },
      expect.anything(),
    );

    // Drive the mutation's onSuccess to verify the navigation side-effect.
    onSuccess?.();

    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("/");
    });
  });

  it("a failed leave says so and keeps the reader in the wizard", async () => {
    // A failed write used to produce nothing at all: no spinner, no message,
    // no navigation, just a button that had apparently done nothing.
    const user = userEvent.setup();
    mutate.mockImplementation(
      (_input: unknown, opts?: { onError?: (reason: unknown) => void }) => {
        opts?.onError?.(new Error("no"));
      },
    );

    customRender(<OnboardingWizardView />);

    await user.click(screen.getByRole("button", { name: /set up later/i }));
    await user.click(
      await screen.findByRole("button", { name: /^leave setup$/i }),
    );

    expect(await screen.findByText(/could not save that/i)).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalledWith("/");
    expect(
      screen.getByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
  });

  it("cannot be dismissed while the leave write is still in the air", async () => {
    // Dismissing does not cancel the write, so a Keep going that still landed
    // on the home page a second later is an answer the reader did not give.
    const user = userEvent.setup();
    mutate.mockImplementation(() => {
      // Never settles: the modal stays in its pending state.
    });
    mockedSettingsMutation.mockReturnValue({
      mutate,
      isPending: true,
    } as unknown as ReturnType<typeof useSettingsMutation>);

    customRender(<OnboardingWizardView />);

    await user.click(screen.getByRole("button", { name: /set up later/i }));
    const keepGoing = await screen.findByRole("button", {
      name: /keep going/i,
    });

    expect(keepGoing).toBeDisabled();
    await user.keyboard("{Escape}");

    expect(
      screen.getByRole("button", { name: /^leave setup$/i }),
    ).toBeInTheDocument();
  });

  it("the providers step can be skipped instead of trapping the reader", async () => {
    // The one step that restarts the application was also the one step with no
    // exit but the permanent skip in the header.
    const user = userEvent.setup();
    localStorage.setItem("bazarr.onboarding.intent", "discover");
    localStorage.setItem("bazarr.onboarding.step", "providers");

    customRender(<OnboardingWizardView />);

    const skip = await screen.findByRole("button", {
      name: /i will pick providers later/i,
    });
    expect(
      screen.queryByText(/nothing for bazarr\+ to fetch subtitles from/i),
    ).not.toBeInTheDocument();

    await user.click(skip);

    expect(
      await screen.findByRole("heading", { name: /subtitle translation/i }),
    ).toBeInTheDocument();
  });

  it("keeps what was typed on a step across Back and forward", async () => {
    // Every step held its fields in its own useState and the shell remounts a
    // step on every move, so re-reading the previous question emptied the form
    // with no warning.
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await answerIntent(user, /sonarr, radarr or sportarr/i);
    await screen.findByRole("heading", { name: /^sonarr$/i });

    await user.type(screen.getByLabelText(/address/i), "10.0.0.5");

    await user.click(screen.getByRole("button", { name: /^back$/i }));
    await screen.findByRole("heading", { name: /what do you want bazarr/i });
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    await screen.findByRole("heading", { name: /^sonarr$/i });
    expect(screen.getByLabelText(/address/i)).toHaveValue("10.0.0.5");
  });
});
