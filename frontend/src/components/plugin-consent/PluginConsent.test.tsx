/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useState } from "react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import {
  installedTranslator,
  installSummary,
  plainSummary,
  quietDelta,
  updateDelta,
} from "./fixtures";
import PluginConsent, { PluginConsentTarget } from "./PluginConsent";
import { usePluginConsentFlow } from "./usePluginConsentFlow";

let calls: Array<{ method: string; path: string; body: unknown }>;

const record = (method: string, path: string, body: unknown = null) => {
  calls.push({ method, path, body });
};

const installTarget: PluginConsentTarget = {
  action: "install",
  pluginId: "ai_translator",
  name: "AI Subtitle Translator",
  source: "official",
  version: "2.0.0",
};

const updateTarget: PluginConsentTarget = {
  action: "update",
  pluginId: "ai_translator",
  name: "AI Subtitle Translator",
};

/** The dialog on its own, opened by one button and nothing else around it. */
const Alone: FunctionComponent<{
  target: PluginConsentTarget;
  costCaps?: { jobCap: number | null; monthCap: number | null };
  onDone?: (target: PluginConsentTarget) => void;
}> = ({ target, costCaps, onDone }) => {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Ask
      </button>
      <PluginConsent
        target={open ? target : null}
        costCaps={costCaps}
        onClose={() => setOpen(false)}
        onDone={onDone}
      />
    </>
  );
};

beforeEach(() => {
  calls = [];
  server.use(
    http.get("/api/system/settings", () => HttpResponse.json({ general: {} })),
    http.get("/api/plugins/catalog/consent", ({ request }) => {
      record(
        "get",
        "install-consent",
        Object.fromEntries(new URL(request.url).searchParams),
      );
      return HttpResponse.json(installSummary);
    }),
    http.post("/api/plugins/installations", async ({ request }) => {
      record("post", "install", await request.json());
      return HttpResponse.json({ job_id: null }, { status: 202 });
    }),
    http.get("/api/plugins/updates/consent", () => {
      record("get", "update-consent");
      return HttpResponse.json(updateDelta);
    }),
    http.get("/api/plugins/installations/:id", () =>
      HttpResponse.json(installedTranslator),
    ),
    http.post("/api/plugins/updates/apply", async ({ request }) => {
      record("post", "apply", await request.json());
      return HttpResponse.json({ job_id: null }, { status: 202 });
    }),
  );
});

/** The list item inside a scope that holds the given text. */
const rowWith = (scope: HTMLElement, text: string) => {
  const found = within(scope)
    .getAllByRole("listitem")
    .find((item) => within(item).queryByText(text));
  if (!found) {
    throw new Error(`no row with ${text}`);
  }
  return found;
};

const open = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(screen.getByRole("button", { name: "Ask" }));
  const dialog = await screen.findByRole("dialog", {
    name: /AI Subtitle Translator\?/,
  });
  // The dialog fades in: what is asserted about its contents waits until it can be seen.
  await waitFor(() => expect(dialog).toBeVisible());
  return dialog;
};

describe("the install dialog, mounted alone", () => {
  it("shows every row of the closure the server describes", async () => {
    const user = userEvent.setup();
    customRender(
      <Alone
        target={installTarget}
        costCaps={{ jobCap: 2.5, monthCap: null }}
      />,
    );
    const dialog = await open(user);
    const group = (name: string) =>
      within(dialog).findByRole("group", { name });

    // Where it comes from, in Bazarr+'s own words for the source.
    const source = await group("Where it comes from");
    expect(within(source).getByText("Official catalog")).toBeVisible();
    expect(within(source).getByText(/which Bazarr\+ trusts/)).toBeVisible();

    expect(
      within(await group("Download size")).getByText("3.1 MB"),
    ).toBeVisible();

    // Permissions, each marked in words.
    const may = await group("What it may do");
    expect(within(may).getByText("Network access")).toBeVisible();
    expect(
      within(rowWith(may, "Network access")).getByText("Enforced"),
    ).toBeVisible();
    expect(
      within(rowWith(may, "Keep its own data folder")).getByText(
        "Declared only",
      ),
    ).toBeVisible();
    expect(
      within(may).getByText(/Declared only means the plugin says/),
    ).toBeVisible();

    // What leaves the machine: each origin.
    const leaves = await group("What leaves this machine");
    expect(within(leaves).getByText("https://openrouter.ai:443")).toBeVisible();
    expect(
      within(leaves).getByText("https://api.example.org:443"),
    ).toBeVisible();

    // A container image and Docker access, with the warning.
    const programs = await group("Programs and containers");
    expect(within(programs).getByText(/Runs a container image/)).toBeVisible();
    expect(within(programs).getByText(/root-equivalent/)).toBeVisible();

    // The closure member, with its own source and permissions.
    const also = await group("Also installed with it");
    expect(within(also).getByText(/Opencode back end 0.4.1/)).toBeVisible();
    expect(within(also).getByText("Some community source")).toBeVisible();
    expect(
      within(also).getByText(/Review it before you install/),
    ).toBeVisible();
    expect(within(also).getByText("Run programs it ships")).toBeVisible();

    expect(
      within(await group("Already installed")).getByText("host_cache"),
    ).toBeVisible();
    expect(
      within(
        await group("Needs something this install does not supply"),
      ).getByText("flaresolverr.v1"),
    ).toBeVisible();
    expect(
      within(await group("Memory")).getByText(/up to 1.5 GB/),
    ).toBeVisible();
    expect(
      within(await group("Spending")).getByText(
        "Cost caps right now: $2.50 per job, no cap set per month.",
      ),
    ).toBeVisible();
    expect(
      within(await group("Removing it")).getByText(/remove it at any time/),
    ).toBeVisible();
  });

  it("asks for the closure of the entry it was given and sends nothing before the person agrees", async () => {
    const user = userEvent.setup();
    customRender(<Alone target={installTarget} />);
    const dialog = await open(user);
    await within(dialog).findByRole("group", { name: "What it may do" });
    expect(calls).toEqual([
      {
        method: "get",
        path: "install-consent",
        body: { source: "official", id: "ai_translator", version: "2.0.0" },
      },
    ]);
    // Cancelling is leaving: no install request ever went out.
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(calls.map((call) => call.path)).toEqual(["install-consent"]);
  });

  it("sends the digest of the set it showed with the install", async () => {
    const user = userEvent.setup();
    let done: PluginConsentTarget | null = null;
    customRender(
      <Alone target={installTarget} onDone={(item) => (done = item)} />,
    );
    const dialog = await open(user);
    const install = await within(dialog).findByRole("button", {
      name: "Install",
    });
    await waitFor(() => expect(install).toBeEnabled());
    await user.click(install);
    await waitFor(() =>
      expect(calls.map((call) => call.path)).toEqual([
        "install-consent",
        "install",
      ]),
    );
    expect(calls[1].body).toEqual({
      source: "official",
      id: "ai_translator",
      version: "2.0.0",
      consent: "sha256:aaaa",
    });
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(done).toEqual(installTarget);
  });

  it("shows a refusal in the server's words, and asks for the set again when it changed", async () => {
    let summary = installSummary;
    server.use(
      http.get("/api/plugins/catalog/consent", () => {
        record("get", "install-consent");
        return HttpResponse.json(summary);
      }),
      http.post("/api/plugins/installations", async ({ request }) => {
        const body = (await request.json()) as { consent: string };
        record("post", "install", body);
        if (body.consent === "sha256:aaaa") {
          // The catalog moved on between the dialog and the install.
          summary = { ...installSummary, digest: "sha256:bbbb" };
          return HttpResponse.json(
            "What this install would add has changed since it was shown to you. Review it again before you agree.",
            { status: 409 },
          );
        }
        return HttpResponse.json({ job_id: null }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    customRender(<Alone target={installTarget} />);
    const dialog = await open(user);
    const install = await within(dialog).findByRole("button", {
      name: "Install",
    });
    await waitFor(() => expect(install).toBeEnabled());
    await user.click(install);
    expect(
      await within(dialog).findByText(/has changed since it was shown to you/),
    ).toBeVisible();
    // The dialog is still up, with the new set read, and nothing was installed.
    await waitFor(() =>
      expect(
        calls.filter((call) => call.path === "install-consent"),
      ).toHaveLength(2),
    );
    await waitFor(() =>
      expect(
        within(dialog).getByRole("button", { name: "Install" }),
      ).toBeEnabled(),
    );
    expect(calls.filter((call) => call.path === "install")).toHaveLength(1);
    // Agreeing again sends the new digest.
    await user.click(within(dialog).getByRole("button", { name: "Install" }));
    await waitFor(() =>
      expect(calls.filter((call) => call.path === "install")).toHaveLength(2),
    );
    expect((calls.at(-1)?.body as { consent: string }).consent).toBe(
      "sha256:bbbb",
    );
  });

  it("shows another refusal as the server worded it and stays open", async () => {
    server.use(
      http.post("/api/plugins/installations", () =>
        HttpResponse.json("Install the AI Subtitle Translator first.", {
          status: 400,
        }),
      ),
    );
    const user = userEvent.setup();
    customRender(<Alone target={installTarget} />);
    const dialog = await open(user);
    const install = await within(dialog).findByRole("button", {
      name: "Install",
    });
    await waitFor(() => expect(install).toBeEnabled());
    await user.click(install);
    expect(
      await within(dialog).findByText(
        "Install the AI Subtitle Translator first.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("says what it could not load, and offers to try again", async () => {
    let attempts = 0;
    server.use(
      http.get("/api/plugins/catalog/consent", () => {
        attempts += 1;
        return attempts === 1
          ? HttpResponse.json("The closure does not resolve on this host.", {
              status: 400,
            })
          : HttpResponse.json(plainSummary);
      }),
    );
    const user = userEvent.setup();
    customRender(<Alone target={installTarget} />);
    const dialog = await open(user);
    // The dialog may still be fading in when the answer arrives.
    const refusal = await within(dialog).findByText(
      "The closure does not resolve on this host.",
    );
    await waitFor(() => expect(refusal).toBeVisible());
    // Nothing can be agreed to that was not shown.
    expect(
      within(dialog).queryByRole("button", { name: "Install" }),
    ).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Try again" }));
    expect(
      await within(dialog).findByText("Nothing is sent from this machine."),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Install" }),
    ).toBeEnabled();
  });
});

describe("focus and keys", () => {
  it("starts on the heading, goes through the buttons in order, and returns to the button that opened it", async () => {
    const user = userEvent.setup();
    customRender(<Alone target={installTarget} />);
    const trigger = screen.getByRole("button", { name: "Ask" });
    const dialog = await open(user);
    const heading = within(dialog).getByText("Install AI Subtitle Translator?");
    await waitFor(() => expect(heading).toHaveFocus());
    const install = await within(dialog).findByRole("button", {
      name: "Install",
    });
    await waitFor(() => expect(install).toBeEnabled());
    // Heading, then the close button, then Cancel, then Install: the one that agrees comes last.
    await user.tab();
    expect(within(dialog).getByRole("button", { name: "Close" })).toHaveFocus();
    await user.tab();
    expect(
      within(dialog).getByRole("button", { name: "Cancel" }),
    ).toHaveFocus();
    await user.tab();
    expect(install).toHaveFocus();
    // Tab stays inside the dialog.
    await user.tab();
    // eslint-disable-next-line testing-library/no-node-access -- asking where focus is.
    const focused = document.activeElement as HTMLElement;
    expect(dialog).toContainElement(focused);
    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(calls.map((call) => call.path)).not.toContain("install");
  });

  it("agrees from the keyboard on the Install button", async () => {
    const user = userEvent.setup();
    customRender(<Alone target={installTarget} />);
    const dialog = await open(user);
    const install = await within(dialog).findByRole("button", {
      name: "Install",
    });
    await waitFor(() => expect(install).toBeEnabled());
    install.focus();
    await user.keyboard("{Enter}");
    await waitFor(() =>
      expect(calls.map((call) => call.path)).toContain("install"),
    );
  });
});

describe("the update dialog", () => {
  it("shows the old release and the new one side by side, and sends the delta's digest", async () => {
    const user = userEvent.setup();
    customRender(<Alone target={updateTarget} />);
    const dialog = await open(user);
    const table = await within(dialog).findByRole("group", {
      name: "What changes",
    });
    expect(within(table).getByText("Installed now")).toBeVisible();
    expect(within(table).getByText("After the update")).toBeVisible();
    expect(within(table).getByText("1.0.0")).toBeVisible();
    expect(within(table).getByText("1.1.0")).toBeVisible();
    // The destination it adds is marked new; the one it had is in both columns.
    expect(
      within(table).getByText("https://api.deepl.com:443 (new)"),
    ).toBeVisible();
    expect(
      within(table).getAllByText("https://openrouter.ai:443"),
    ).toHaveLength(2);
    expect(
      within(table).getByText("Read your media folders (new)"),
    ).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Update" }));
    await waitFor(() =>
      expect(calls.map((call) => call.path)).toContain("apply"),
    );
    expect(calls.find((call) => call.path === "apply")?.body).toEqual({
      id: "ai_translator",
      consent: "sha256:delta",
    });
  });
});

describe("an update through the flow", () => {
  const Flow: FunctionComponent = () => {
    const { begin, dialog } = usePluginConsentFlow();
    return (
      <>
        <button type="button" onClick={() => void begin(updateTarget)}>
          Update it
        </button>
        {dialog}
      </>
    );
  };

  it("goes ahead on its own when the update adds nothing to consent to", async () => {
    server.use(
      http.get("/api/plugins/updates/consent", () => {
        record("get", "update-consent");
        return HttpResponse.json(quietDelta);
      }),
    );
    const user = userEvent.setup();
    customRender(<Flow />);
    await user.click(screen.getByRole("button", { name: "Update it" }));
    await waitFor(() =>
      expect(calls.map((call) => call.path)).toEqual([
        "update-consent",
        "apply",
      ]),
    );
    expect(calls[1].body).toEqual({
      id: "ai_translator",
      consent: "sha256:quiet",
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens the dialog, and sends nothing, when the update adds something", async () => {
    const user = userEvent.setup();
    customRender(<Flow />);
    await user.click(screen.getByRole("button", { name: "Update it" }));
    const dialog = await screen.findByRole("dialog", {
      name: /Update AI Subtitle Translator\?/,
    });
    await within(dialog).findByRole("group", { name: "What changes" });
    expect(calls.map((call) => call.path)).not.toContain("apply");
  });
});
