import userEvent, { UserEvent } from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { compileString } from "sass-embedded";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import OnboardingWizardView from "./OnboardingWizard";
import { settleSetupComplete } from "./setupCompleteCache";
import wizardStyles from "./OnboardingWizard.module.scss";

// Navigation + the settings mutation are the only external effects we assert.
const navigate = vi.fn();
const mutate = vi.fn();
// What the address bar says. The shell reads the step key off the route, so a
// test that stands the wizard on a mid step has to say so here as well.
let routeParams: { stepKey?: string } = {};

vi.mock("react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router")>();
  return {
    ...actual,
    useNavigate: () => navigate,
    useParams: () => routeParams,
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

// The real cache settle unless a test holds it open, which is how the window
// between the write landing and the settings read coming back is staged.
vi.mock("./setupCompleteCache", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./setupCompleteCache")>();
  return {
    ...actual,
    settleSetupComplete: vi.fn(actual.settleSetupComplete),
  };
});

const mockedSettingsMutation = vi.mocked(useSettingsMutation);

// Holds the next cache settle open until the returned function is called.
function holdSettle(): () => void {
  let settled: (() => void) | undefined;
  vi.mocked(settleSetupComplete).mockImplementationOnce(
    () =>
      new Promise<void>((resolve) => {
        settled = resolve;
      }),
  );
  return () => settled?.();
}

// A write that lands at once, so what is left pending is the cache settle.
function writeLandsAtOnce() {
  mutate.mockImplementation(
    (_input: unknown, opts?: { onSuccess?: () => unknown }) => {
      void opts?.onSuccess?.();
    },
  );
}

describe("OnboardingWizardView", () => {
  beforeEach(() => {
    localStorage.clear();
    routeParams = {};
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
    // The dialog's close icon has a name, not an empty button.
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();

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

  it("leaving from a mid step clears the cursor for good", async () => {
    // The reset cleared the cursor and the URL sync effect wrote it straight
    // back, because the URL still named the step being left and the list still
    // knew it. Settings, General then reopened setup on that step.
    const user = userEvent.setup();
    let onSuccess: (() => void) | undefined;
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        onSuccess = opts?.onSuccess;
      },
    );

    localStorage.setItem("bazarr.onboarding.intent", "library");
    localStorage.setItem("bazarr.onboarding.step", "sonarr");
    routeParams = { stepKey: "sonarr" };

    const view = customRender(<OnboardingWizardView />);
    expect(
      await screen.findByRole("heading", { name: /sonarr/i }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /set up later/i }));
    await user.click(
      await screen.findByRole("button", { name: /^leave setup$/i }),
    );
    onSuccess?.();

    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("/");
    });
    // The wizard keeps rendering until the navigation unmounts it, so the
    // effect that used to resurrect the cursor has had its chance by now.
    expect(localStorage.getItem("bazarr.onboarding.step")).toBeNull();

    view.unmount();
    routeParams = {};
    customRender(<OnboardingWizardView />);

    expect(
      await screen.findByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
    expect(localStorage.getItem("bazarr.onboarding.step")).toBeNull();
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

  it("cannot be dismissed while the settings read is still settling", async () => {
    // The write answers before the cache settles, and the settle is what the
    // navigation waits for. Only the write counted as pending, so for that
    // second wait Keep going, Escape and the close button all worked, and the
    // reader who used one still landed on the home page.
    const user = userEvent.setup();
    const settle = holdSettle();
    writeLandsAtOnce();

    customRender(<OnboardingWizardView />);

    await user.click(screen.getByRole("button", { name: /set up later/i }));
    await user.click(
      await screen.findByRole("button", { name: /^leave setup$/i }),
    );

    expect(mutate).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /keep going/i })).toBeDisabled();
    // Loading, so a second press cannot write again.
    expect(
      screen.getByRole("button", { name: /^leave setup$/i }),
    ).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(
      screen.getByRole("button", { name: /^leave setup$/i }),
    ).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalledWith("/");

    settle();

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/"));
    expect(navigate.mock.calls.filter(([to]) => to === "/").length).toBe(1);
    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it("Finish takes Back and Set up later away until it has left", async () => {
    // Back unmounted Finish while its write or the settle after it was still
    // going, and the success that followed cleared the wizard and navigated
    // away from under the step the reader had gone back to.
    const user = userEvent.setup();
    localStorage.setItem("bazarr.onboarding.intent", "discover");
    localStorage.setItem("bazarr.onboarding.step", "finish");
    server.use(
      http.get("/api/system/media-server-instances", () =>
        HttpResponse.json({ data: [] }),
      ),
    );
    const settle = holdSettle();
    writeLandsAtOnce();

    customRender(<OnboardingWizardView />);

    await screen.findByRole("heading", { name: /you are all set/i });
    expect(screen.getByRole("button", { name: /^back$/i })).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /finish and open discover/i }),
    );

    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /^back$/i }),
      ).not.toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: /set up later/i }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /finish and open discover/i }),
    ).toBeDisabled();
    expect(navigate).not.toHaveBeenCalledWith("/discover");

    settle();

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/discover"));
    expect(mutate).toHaveBeenCalledTimes(1);
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

  it('never has a "we" do the work on Welcome or the intent question', async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    expect(
      screen.getByText(/setup only asks for what that needs/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/\bwe\b/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /get started/i }));
    await screen.findByRole("heading", { name: /what do you want bazarr/i });
    expect(screen.queryByText(/\bwe\b/i)).not.toBeInTheDocument();
  });

  it("asks the intent question on one axis and says what each answer changes", async () => {
    // One answer said what the reader owned and the other what they wanted,
    // so a Sonarr user recognised themselves in both, and the helper never
    // said which steps the answer decided.
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);
    await user.click(screen.getByRole("button", { name: /get started/i }));

    expect(
      await screen.findByRole("radio", {
        name: "Fetch subtitles for my Sonarr, Radarr or Sportarr library",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("radio", {
        name: "Find subtitles for anything, on demand",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /only decides whether setup asks for sonarr, radarr and sportarr/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/setup asks you to connect them next/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/leaves out the sonarr, radarr and sportarr steps/i),
    ).toBeInTheDocument();
  });

  it("names the page with one h1 and puts the step in a main landmark", () => {
    customRender(<OnboardingWizardView />);

    expect(
      screen.getByRole("heading", { level: 1, name: "Bazarr+ Setup" }),
    ).toBeInTheDocument();
    const main = screen.getByRole("main");
    expect(
      within(main).getByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
  });

  it("reports progress as a progressbar instead of a hidden rail", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    const rail = screen.getByRole("progressbar", { name: "Setup progress" });
    expect(rail).not.toHaveAttribute("aria-hidden");
    expect(rail).toHaveAttribute("aria-valuemin", "1");
    expect(rail).toHaveAttribute("aria-valuemax", "4");
    expect(rail).toHaveAttribute("aria-valuenow", "1");
    expect(rail).toHaveAttribute(
      "aria-valuetext",
      "Part 1 of 4: Getting started, 1 of 2",
    );
    // A title is a mouse-hover label, and the value text already names the
    // phase for everyone else.
    expect(within(rail).queryByTitle(/.+/)).not.toBeInTheDocument();

    await answerIntent(user, /sonarr, radarr or sportarr/i);
    await screen.findByRole("heading", { name: /^sonarr$/i });

    expect(rail).toHaveAttribute("aria-valuenow", "2");
    expect(rail).toHaveAttribute(
      "aria-valuetext",
      "Part 2 of 4: Connect, 1 of 5",
    );
  });

  it("takes no focus and announces nothing on first render", () => {
    customRender(<OnboardingWizardView />);

    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(document.body).toHaveFocus();
    expect(document.title).toBe("Welcome - Setup - Bazarr+");
  });

  it("moves focus to the new step's heading and announces the step", async () => {
    // Focus used to drop to the body on every step, so a keyboard reader
    // started from the top of the document each time, and nothing was said.
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await user.click(screen.getByRole("button", { name: /get started/i }));

    const heading = await screen.findByRole("heading", {
      name: /what do you want bazarr/i,
    });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(screen.getByRole("status")).toHaveTextContent(
      "Your setup. Getting started, 2 of 2",
    );
    expect(document.title).toBe("Your setup - Setup - Bazarr+");

    // Tab goes on from the heading into the step, not from the top.
    await user.tab();
    expect(
      screen.getByRole("radio", { name: /sonarr, radarr or sportarr/i }),
    ).toHaveFocus();

    await user.click(screen.getByRole("radio", { name: /find subtitles/i }));
    await user.click(screen.getByRole("button", { name: /^continue$/i }));
    const servers = await screen.findByRole("heading", {
      name: /^media servers$/i,
    });
    await waitFor(() => expect(servers).toHaveFocus());
    expect(screen.getByRole("status")).toHaveTextContent(
      "Media servers. Connect, Media servers 1 of 1",
    );
  });

  it("reports a rejected instance create once, inline, with no client toast", async () => {
    // Through the real hooks and the real API client: the client toasts
    // "Error 400" for any failed request, so a create the backend refused
    // used to say so twice, once beside Continue and once in the corner.
    server.use(
      http.post("/api/system/arr-instances", () =>
        HttpResponse.json({ message: "Port is not a number" }, { status: 400 }),
      ),
    );
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await answerIntent(user, /sonarr, radarr or sportarr/i);
    await screen.findByRole("heading", { name: /^sonarr$/i });
    await user.type(screen.getByLabelText(/^address/i), "10.0.0.5");
    await user.type(screen.getByLabelText(/api key/i), "abc123");
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Port is not a number",
    );
    // Give a toast every chance to arrive before saying there is none.
    await new Promise((settle) => setTimeout(settle, 300));
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.queryByText("Error 400")).not.toBeInTheDocument();
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

// jsdom applies no stylesheet and computes no colour, so these read the CSS
// the build actually emits: each stylesheet is compiled with the same Sass and
// the same injected modules as vite.config.ts, then parsed into rules, and the
// assertions follow a value from the rule that paints a control to the token
// it reads and the colour that token holds. The computed result in a real
// browser is measured separately (see the lane's measurement harness).
interface CssRule {
  selectors: string[];
  at: string[];
  decls: Record<string, string>;
}

function compileScss(file: string): string {
  const root = process.cwd();
  const inject = `@use "${root}/src/assets/_mantine" as mantine;\n@use "${root}/src/assets/_bazarr" as bazarr;\n`;
  const source = readFileSync(file, "utf8");
  return compileString(
    file.endsWith("_bazarr.scss") ? source : inject + source,
    { url: pathToFileURL(resolve(root, file)), style: "expanded" },
  ).css;
}

function parseRules(css: string, at: string[] = []): CssRule[] {
  const found: CssRule[] = [];
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  let cursor = 0;
  while (cursor < text.length) {
    const open = text.indexOf("{", cursor);
    if (open < 0) break;
    let depth = 0;
    let close = open;
    for (; close < text.length; close += 1) {
      if (text[close] === "{") depth += 1;
      else if (text[close] === "}" && --depth === 0) break;
    }
    const prelude = text.slice(cursor, open).trim();
    const body = text.slice(open + 1, close);
    if (prelude.startsWith("@")) {
      found.push(...parseRules(body, [...at, prelude]));
    } else {
      const decls: Record<string, string> = {};
      for (const decl of body.split(";")) {
        const colon = decl.indexOf(":");
        if (colon > 0) {
          decls[decl.slice(0, colon).trim()] = decl.slice(colon + 1).trim();
        }
      }
      found.push({
        selectors: prelude.split(",").map((part) => part.trim()),
        at,
        decls,
      });
    }
    cursor = close + 1;
  }
  return found;
}

const themeRules = parseRules(compileScss("src/assets/_bazarr.scss"));
const inputRules = parseRules(compileScss("src/assets/input.module.scss"));
const wizardRules = parseRules(
  compileScss("src/pages/Setup/OnboardingWizard.module.scss"),
);
const brandSource = readFileSync("src/assets/_bazarr.scss", "utf8");

function rulesFor(rules: CssRule[], selector: string): CssRule[] {
  return rules.filter((rule) => rule.selectors.includes(selector));
}

/** A property as the rules that name this exact selector set it, last wins. */
function declared(
  rules: CssRule[],
  selector: string,
  property: string,
): string | undefined {
  return rulesFor(rules, selector)
    .map((rule) => rule.decls[property])
    .filter((value) => value !== undefined)
    .at(-1);
}

function schemeToken(scheme: "dark" | "light", token: string): string {
  const selector = `:root[data-mantine-color-scheme=${scheme}]`;
  const value =
    declared(themeRules, selector, token) ??
    declared(themeRules, `:root[data-mantine-color-scheme="${scheme}"]`, token);
  return value ?? "";
}

// Mantine's default primary shade: 6 in light mode, 8 in dark. ThemeProvider
// does not change it.
const PRIMARY_SHADE = { light: "6", dark: "8" } as const;

/** A colour value resolved to hex through the tokens and the brand scale. */
function resolveColour(value: string, scheme: "dark" | "light"): string {
  const trimmed = value.trim();
  if (/^#[0-9a-f]{6}$/i.test(trimmed)) return trimmed.toLowerCase();
  if (/^(white|#fff)$/i.test(trimmed)) return "#ffffff";
  const ref = trimmed.match(/^var\((--[\w-]+)\)$/)?.[1];
  if (ref === undefined) return "";
  if (ref === "--mantine-color-white") return "#ffffff";
  if (ref === "--mantine-primary-color-filled") {
    return brandHex(PRIMARY_SHADE[scheme]);
  }
  const brand = ref.match(/^--mantine-color-brand-(\d)$/)?.[1];
  if (brand !== undefined) return brandHex(brand);
  return resolveColour(schemeToken(scheme, ref), scheme);
}

function brandHex(shade: string): string {
  return (
    brandSource
      .match(new RegExp(`\\$color-brand-${shade}: (#[0-9a-f]{6});`))?.[1]
      ?.toLowerCase() ?? ""
  );
}

function luminance(hex: string): number {
  const channels = [1, 3, 5].map(
    (at) => parseInt(hex.slice(at, at + 2), 16) / 255,
  );
  const [r, g, b] = channels.map((c) =>
    c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  expect(a).toMatch(/^#[0-9a-f]{6}$/);
  expect(b).toMatch(/^#[0-9a-f]{6}$/);
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** Every surface the wizard paints behind its controls, read from its rules. */
function wizardSurfaces(scheme: "dark" | "light"): string[] {
  const painted = [
    declared(wizardRules, ".card", "background"),
    declared(wizardRules, ".root", "background"),
  ].join(" ");
  const tokens = [...painted.matchAll(/var\((--bz-surface-[\w-]+)\)/g)].map(
    (match) => match[1],
  );
  expect(tokens).toContain("--bz-surface-card");
  return tokens.map((token) => resolveColour(`var(${token})`, scheme));
}

const FOCUS_SELECTORS = [
  ".mantine-focus-auto:focus-visible",
  ".mantine-focus-always:focus",
  ".mantine-Switch-input:focus-visible + .mantine-Switch-track",
  ".mantine-Chip-input:focus-visible + .mantine-Chip-label",
];

describe("focus indicators", () => {
  it.each(["dark", "light"] as const)(
    "the %s focus ring clears 3:1 on every surface the wizard paints",
    (scheme) => {
      const ring = resolveColour("var(--bz-focus-ring)", scheme);
      for (const surface of wizardSurfaces(scheme)) {
        // WCAG 2.2 SC 1.4.11. The dark ring was brand-8, 2.05:1 on the card.
        expect(contrast(ring, surface)).toBeGreaterThanOrEqual(3);
      }
    },
  );

  it.each(FOCUS_SELECTORS)(
    "%s draws its ring in the token, outside any layer",
    (selector) => {
      // Global by design: _bazarr.scss is compiled into every module.
      const rule = rulesFor(themeRules, `:global ${selector}`).find(
        (candidate) => candidate.decls["outline-color"] !== undefined,
      );
      expect(rule?.decls["outline-color"]).toBe("var(--bz-focus-ring)");
      // Unlayered, or Mantine's own layered rule would win.
      expect(rule?.at.some((at) => at.startsWith("@layer"))).toBe(false);
    },
  );

  it.each(["dark", "light"] as const)(
    "gives a focused %s input an indicator that clears 3:1",
    (scheme) => {
      // Tab used to change only an overlay alpha, 0.06 to 0.07: 1.03:1.
      for (const selector of [".input:focus", ".input:focus-within"]) {
        expect(declared(inputRules, selector, "border-color")).toBe(
          "var(--bz-focus-ring)",
        );
        expect(declared(inputRules, selector, "box-shadow")).toBe(
          "0 0 0 1px var(--bz-focus-ring)",
        );
      }
      const ring = resolveColour("var(--bz-focus-ring)", scheme);
      for (const surface of wizardSurfaces(scheme)) {
        expect(contrast(ring, surface)).toBeGreaterThanOrEqual(3);
      }
    },
  );

  it("reaches every Mantine input through the theme", () => {
    const provider = readFileSync("src/App/ThemeProvider.tsx", "utf8");
    expect(provider).toMatch(
      /import inputClasses from "@\/assets\/input\.module\.scss";/,
    );
    expect(provider).toMatch(
      /Input: Input\.extend\(\{\s*classNames: inputClasses,\s*\}\)/,
    );
  });
});

// The palettes ThemeProvider hands Mantine, read from its source.
const providerSource = readFileSync("src/App/ThemeProvider.tsx", "utf8");
function paletteHex(name: string, shade: number): string {
  const block = providerSource.match(
    new RegExp(`\\b${name}: \\[([^\\]]+)\\]`),
  )?.[1];
  return (block?.match(/#[0-9a-fA-F]{6}/g)?.[shade] ?? "").toLowerCase();
}

function overrideFor(scheme: "dark" | "light", token: string): string {
  return (
    declared(
      themeRules,
      `:root:root[data-mantine-color-scheme=${scheme}]`,
      token,
    ) ?? ""
  );
}

function paletteRef(value: string): string {
  const match = value.match(/^var\(--mantine-color-(\w+)-(\d)\)$/);
  if (match === null) return "";
  return match[1] === "brand"
    ? brandHex(match[2])
    : paletteHex(match[1], Number(match[2]));
}

describe("small text colours", () => {
  it.each(["dark", "light"] as const)(
    "a %s field error clears 4.5:1 on every surface the wizard paints",
    (scheme) => {
      // Measured: red-8 was 2.2:1 on the dark card, red-6 4.26:1 on white.
      const error = paletteRef(overrideFor(scheme, "--mantine-color-error"));
      for (const surface of wizardSurfaces(scheme)) {
        expect(contrast(error, surface)).toBeGreaterThanOrEqual(4.5);
      }
    },
  );

  it("a light yellow alert's title clears 4.5:1 on its own fill", () => {
    // Measured: yellow-9 on yellow-1 was 2.69:1.
    const title = paletteRef(
      overrideFor("light", "--mantine-color-yellow-light-color"),
    );
    for (const fill of [paletteHex("yellow", 1), "#ffffff"]) {
      expect(contrast(title, fill)).toBeGreaterThanOrEqual(4.5);
    }
  });
});

describe("rail numerals", () => {
  it.each([".dotActive", ".dotDone"])(
    "%s keeps white numerals at 4.5:1 in both schemes",
    (dot) => {
      // 13px semibold is not large text, so it needs 4.5:1. White on the
      // light filled shade, brand-6, was 4.2:1.
      expect(declared(wizardRules, dot, "background")).toBe("var(--dot-fill)");
      const numeral = resolveColour(
        declared(wizardRules, dot, "color") ?? "",
        "light",
      );
      const darkFill = declared(wizardRules, dot, "--dot-fill") ?? "";
      const lightFill =
        declared(
          wizardRules,
          `[data-mantine-color-scheme=light] ${dot}`,
          "--dot-fill",
        ) ?? darkFill;
      expect(
        contrast(numeral, resolveColour(darkFill, "dark")),
      ).toBeGreaterThanOrEqual(4.5);
      expect(
        contrast(numeral, resolveColour(lightFill, "light")),
      ).toBeGreaterThanOrEqual(4.5);
    },
  );

  it("marks the dot of the phase the reader is in", () => {
    customRender(<OnboardingWizardView />);
    const rail = screen.getByRole("progressbar", { name: "Setup progress" });
    expect(rail).toHaveAttribute("aria-valuenow", "1");
    // The first dot is the active one, and only it.
    const dots = within(rail).getAllByText(/^[1-4]$/);
    expect(dots[0]).toHaveClass(wizardStyles.dotActive);
    for (const dot of dots.slice(1)) {
      expect(dot).not.toHaveClass(wizardStyles.dotActive);
    }
  });
});

describe("touch targets on a coarse pointer", () => {
  const coarse = wizardRules.filter((rule) =>
    rule.at.includes("@media (pointer: coarse)"),
  );
  const scoped = (selector: string) =>
    `:global :root[data-bz-setup] ${selector}`;

  it("floors buttons of every size the wizard uses, compact included", () => {
    for (const size of [
      "xs",
      "sm",
      "md",
      "compact-xs",
      "compact-sm",
      "compact-md",
    ]) {
      expect(
        declared(
          coarse,
          scoped(".mantine-Button-root"),
          `--button-height-${size}`,
        ),
      ).toBe("44px");
    }
    expect(declared(coarse, scoped(".mantine-Button-root"), "min-height")).toBe(
      "44px",
    );
    expect(declared(coarse, scoped(".mantine-Button-root"), "min-width")).toBe(
      "44px",
    );
  });

  it("floors inputs, icons, links, toggles and options at 44px", () => {
    for (const size of ["xs", "sm", "md"]) {
      expect(
        declared(
          coarse,
          scoped(".mantine-Input-wrapper"),
          `--input-height-${size}`,
        ),
      ).toBe("44px");
    }
    for (const selector of [
      ".mantine-ActionIcon-root",
      ".mantine-CloseButton-root:not(.mantine-Pill-remove)",
    ]) {
      expect(declared(coarse, scoped(selector), "min-width")).toBe("44px");
      expect(declared(coarse, scoped(selector), "min-height")).toBe("44px");
    }
    for (const selector of [
      ".mantine-Anchor-root",
      ".mantine-Checkbox-body",
      ".mantine-Radio-body",
      ".mantine-Switch-body",
      ".mantine-Checkbox-label",
      ".mantine-Radio-label",
      ".mantine-Switch-label",
      "[role=option]",
    ]) {
      expect(declared(coarse, scoped(selector), "min-height")).toBe("44px");
    }
    expect(
      declared(coarse, scoped(".mantine-Pill-remove::after"), "inset"),
    ).toBe("-12px");
  });

  it("leaves the fine-pointer sizes alone", () => {
    const outside = wizardRules.filter(
      (rule) => !rule.at.includes("@media (pointer: coarse)"),
    );
    for (const rule of outside) {
      expect(Object.values(rule.decls)).not.toContain("44px");
    }
  });

  it("scopes the floor to the document only while the wizard is mounted", () => {
    const { unmount } = customRender(<OnboardingWizardView />);
    expect(document.documentElement).toHaveAttribute("data-bz-setup");
    unmount();
    expect(document.documentElement).not.toHaveAttribute("data-bz-setup");
  });
});
