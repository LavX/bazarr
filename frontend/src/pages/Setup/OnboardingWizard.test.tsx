import userEvent, { UserEvent } from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import OnboardingWizardView from "./OnboardingWizard";
import { settleSetupComplete } from "./setupCompleteCache";

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

// jsdom applies no stylesheet and computes no colour, so the indicators are
// pinned where they are declared: the tokens and the rules that read them.
const themeSource = readFileSync("src/assets/_bazarr.scss", "utf8");
const inputSource = readFileSync("src/assets/input.module.scss", "utf8");

function schemeBlock(scheme: "dark" | "light"): string {
  const marker = `:root[data-mantine-color-scheme="${scheme}"] {`;
  const at = themeSource.indexOf(marker);
  return at < 0 ? "" : themeSource.slice(at, themeSource.indexOf("}", at));
}

function brandHex(shade: string): string {
  return (
    themeSource.match(
      new RegExp(`\\$color-brand-${shade}: (#[0-9a-f]{6});`),
    )?.[1] ?? ""
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
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function focusRingHex(scheme: "dark" | "light"): string {
  const shade = schemeBlock(scheme).match(
    /--bz-focus-ring: var\(--mantine-color-brand-(\d)\);/,
  )?.[1];
  return shade === undefined ? "" : brandHex(shade);
}

describe("focus indicators", () => {
  it.each([
    ["dark", ["#22223a", "#1a1a2e", "#121125"]],
    ["light", ["#ffffff", "#f8f9fa", "#f1f3f5"]],
  ] as const)(
    "the %s focus ring clears 3:1 on every surface the wizard draws",
    (scheme, surfaces) => {
      const ring = focusRingHex(scheme);
      expect(ring).toMatch(/^#[0-9a-f]{6}$/);
      for (const surface of surfaces) {
        // WCAG 2.2 SC 1.4.11. The dark ring was brand-8, 2.05:1 on the card.
        expect(contrast(ring, surface)).toBeGreaterThanOrEqual(3);
      }
    },
  );

  it("draws Mantine's focus ring in the token, not the primary shade", () => {
    expect(themeSource).toMatch(
      /\.mantine-focus-auto:focus-visible,[\s\S]*?outline-color: var\(--bz-focus-ring\);/,
    );
  });

  it("gives a focused input a visible indicator of its own", () => {
    // Tab used to change only an overlay alpha, 0.06 to 0.07: 1.03:1.
    const focus = inputSource.slice(inputSource.indexOf("&:focus,"));
    expect(focus).toMatch(/border-color: var\(--bz-focus-ring\);/);
    expect(focus).toMatch(/box-shadow: 0 0 0 1px var\(--bz-focus-ring\);/);
  });
});

describe("rail numerals", () => {
  const wizardSource = readFileSync(
    "src/pages/Setup/OnboardingWizard.module.scss",
    "utf8",
  );

  it("fill the light-mode dots dark enough for white 13px numerals", () => {
    const block = wizardSource.slice(wizardSource.indexOf(".dotDone,"));
    const light = block.match(
      /@include mantine\.light \{\s*--dot-fill: var\(--mantine-color-brand-(\d)\);/,
    )?.[1];
    expect(light).toBeDefined();
    // 13px semibold is not large text, so it needs 4.5:1. brand-6 was 4.2:1.
    expect(contrast("#ffffff", brandHex(light ?? ""))).toBeGreaterThanOrEqual(
      4.5,
    );
    expect(block).toMatch(/background: var\(--dot-fill\);/);
    expect(block).toMatch(/color: var\(--mantine-color-white\);/);
  });
});

describe("touch targets on a coarse pointer", () => {
  const wizardSource = readFileSync(
    "src/pages/Setup/OnboardingWizard.module.scss",
    "utf8",
  );

  // The block that carries the floor. jsdom applies no media query and
  // computes no layout, so the stylesheet is what can be asserted.
  function coarseBlock(): string {
    const at = wizardSource.indexOf("@media (pointer: coarse)");
    if (at < 0) return "";
    let depth = 0;
    for (let i = wizardSource.indexOf("{", at); i < wizardSource.length; i++) {
      if (wizardSource[i] === "{") depth += 1;
      else if (wizardSource[i] === "}" && --depth === 0) {
        return wizardSource.slice(at, i + 1);
      }
    }
    return "";
  }

  function rule(block: string, selector: string): string {
    const at = block.indexOf(`${selector} {`);
    return at < 0 ? "" : block.slice(at, block.indexOf("}", at));
  }

  it("floors every kind of control the wizard draws at 44px", () => {
    const block = coarseBlock();
    expect(block).toContain(":root[data-bz-setup]");
    for (const variable of [
      "--input-height-xs",
      "--input-height-sm",
      "--input-height-md",
    ]) {
      expect(rule(block, ".mantine-Input-wrapper")).toContain(
        `${variable}: 44px;`,
      );
    }
    for (const variable of [
      "--button-height-xs",
      "--button-height-sm",
      "--button-height-md",
    ]) {
      expect(rule(block, ".mantine-Button-root")).toContain(
        `${variable}: 44px;`,
      );
    }
    expect(
      rule(block, ".mantine-CloseButton-root:not(.mantine-Pill-remove)"),
    ).toMatch(/min-width: 44px;\s*min-height: 44px;/);
    for (const selector of [
      ".mantine-Anchor-root",
      ".mantine-Switch-body",
      ".mantine-Switch-label",
      ".mantine-Combobox-option",
    ]) {
      expect(rule(block, selector)).toContain("min-height: 44px;");
    }
    expect(block).toMatch(/\.mantine-Pill-remove \{[\s\S]*?inset: -12px;/);
  });

  it("leaves the fine-pointer sizes alone", () => {
    const outside = wizardSource.replace(coarseBlock(), "");
    expect(outside).not.toMatch(/:\s*44px/);
  });

  it("scopes the floor to the document only while the wizard is mounted", () => {
    const { unmount } = customRender(<OnboardingWizardView />);
    expect(document.documentElement).toHaveAttribute("data-bz-setup");
    unmount();
    expect(document.documentElement).not.toHaveAttribute("data-bz-setup");
  });
});
