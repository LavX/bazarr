/* eslint-disable camelcase -- API fixtures use the server's field names. */
/**
 * The mass translate form reads the translation gate from the system status.
 * A closed gate disables the action and shows the gate's reason, and no
 * engine the form does not recognise is labelled "Google Translate".
 */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import MassTranslateForm, {
  type WantedMovieItem,
} from "@/components/forms/MassTranslateForm";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import {
  choose,
  optionsOf,
  PICK_INSTALLATIONS,
  refreshPickInstallations,
  stateLoaded,
  translatorStatus,
} from "./pickFixtures";

const LANGUAGES = [
  { code2: "en", code3: "eng", name: "English", enabled: true },
  { code2: "hu", code3: "hun", name: "Hungarian", enabled: true },
];

const MOVIE: WantedMovieItem = {
  type: "movie",
  radarrId: 9,
  title: "Example",
};

function configure(
  translator_type: string,
  availability: { available: boolean; reason: string },
) {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        translator: { translator_type },
      }),
    ),
    http.get("/api/system/status", () =>
      HttpResponse.json({
        data: { translation_availability: availability },
      }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json(LANGUAGES)),
  );
}

describe("MassTranslateForm and the translation gate", () => {
  it("disables the action and shows the reason when the gate is closed", async () => {
    configure("none", {
      available: false,
      reason: "No translator is configured",
    });
    customRender(<MassTranslateForm items={[MOVIE]} />);

    expect(
      await screen.findByText("No translator is configured"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Translate 1 Item(s)" }),
    ).toBeDisabled();
  });

  it("does not label an engine it does not recognise Google Translate", async () => {
    configure("weird_engine", {
      available: false,
      reason: "Unknown translator type",
    });
    customRender(<MassTranslateForm items={[MOVIE]} />);

    expect(
      await screen.findByText("Unknown translator type"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Google Translate/)).not.toBeInTheDocument();
  });

  it("keeps the action enabled and names the engine when the gate is open", async () => {
    configure("openrouter", { available: true, reason: "" });
    customRender(<MassTranslateForm items={[MOVIE]} />);

    expect(
      await screen.findByText(/AI Subtitle Translator/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Translate 1 Item(s)" }),
    ).toBeEnabled();
  });

  it("answers a refusal with the server's reason when the cached status said open", async () => {
    // The gate answer the form reads is a cached status, and the gate can
    // close elsewhere after it was read: the submit has to answer with the
    // server's own reason, and the next status answer has to disable the
    // action the stale one left enabled.
    const user = userEvent.setup();
    // The settings and languages handlers come from the shared configure; the
    // status handler after them takes precedence, so its two answers decide
    // what the form reads.
    configure("openrouter", { available: true, reason: "" });
    const statusAnswers = [
      { available: true, reason: "" },
      { available: false, reason: "No translator is configured" },
    ];
    server.use(
      http.get("/api/system/status", () =>
        HttpResponse.json({
          data: { translation_availability: statusAnswers.shift() },
        }),
      ),
      // The batch endpoint answers the gate's refusal with an error field
      // and 409, which is the body the refusal reads. The reason differs
      // from the status answer's so the toast is caught reading the 409's
      // own body, not the refetched status.
      http.post("/api/subtitles/batch", () =>
        HttpResponse.json(
          { error: "Translation was switched off" },
          { status: 409 },
        ),
      ),
    );
    customRender(<MassTranslateForm items={[MOVIE]} />);

    // The comboboxes have no options until the languages query settles, so
    // the interaction waits for it instead of clicking an empty dropdown.
    await waitFor(() =>
      expect(
        queryClient.getQueryState([
          QueryKeys.System,
          QueryKeys.Languages,
          false,
        ])?.status,
      ).toBe("success"),
    );
    // Each combobox keeps its dropdown mounted, and jsdom puts the options
    // outside the accessibility tree it computes, so the option is picked
    // inside the combobox's own dropdown, hidden elements included.
    const sourceCombobox = screen.getByRole("combobox", {
      name: "Source Language",
    });
    await user.click(sourceCombobox);
    const sourceDropdown = await waitFor(() => {
      // The dropdown lives outside the rendered container's accessibility
      // tree, so it is reached through the combobox's own controls id.
      // eslint-disable-next-line testing-library/no-node-access
      const el = document.getElementById(
        sourceCombobox.getAttribute("aria-controls") ?? "",
      );
      expect(el).not.toBeNull();
      return el!;
    });
    await user.click(
      within(sourceDropdown).getByRole("option", {
        name: "English",
        hidden: true,
      }),
    );
    const targetCombobox = screen.getByRole("combobox", {
      name: "Target Language",
    });
    await user.click(targetCombobox);
    const targetDropdown = await waitFor(() => {
      // eslint-disable-next-line testing-library/no-node-access
      const el = document.getElementById(
        targetCombobox.getAttribute("aria-controls") ?? "",
      );
      expect(el).not.toBeNull();
      return el!;
    });
    await user.click(
      within(targetDropdown).getByRole("option", {
        name: "Hungarian",
        hidden: true,
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "Translate 1 Item(s)" }),
    );

    // The 409's own reason, which only the toast can show: the gate alert
    // reads the status answer's reason, a different sentence. The raw
    // transport error the catch used to show is gone.
    expect(
      await screen.findByText("Translation was switched off"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/AxiosError/)).not.toBeInTheDocument();

    // The refusal also refetches the status, so the action reads the closed
    // gate and disables itself.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Translate 1 Item(s)" }),
      ).toBeDisabled(),
    );
  });
});

describe("MassTranslateForm and the manual pick", () => {
  function settings() {
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { theme: "auto" },
          translator: { translator_type: "google_translate" },
        }),
      ),
      http.get("/api/system/languages", () => HttpResponse.json(LANGUAGES)),
    );
  }

  async function languagesLoaded() {
    await waitFor(() =>
      expect(
        queryClient.getQueryState([
          QueryKeys.System,
          QueryKeys.Languages,
          false,
        ])?.status,
      ).toBe("success"),
    );
  }

  it("offers the back ends that can take a job, and no others", async () => {
    const user = userEvent.setup();
    settings();
    translatorStatus(true, { available: true, reason: "" });
    customRender(<MassTranslateForm items={[MOVIE]} />);

    await screen.findByRole("combobox", { name: "Translate with" });
    await stateLoaded();
    expect(await optionsOf(user, "Translate with")).toEqual([
      "Automatic",
      "OpenAI compatible, usable-model",
      "Google Translate",
    ]);
  });

  it("sends the picked back end in the batch options", async () => {
    const user = userEvent.setup();
    settings();
    translatorStatus(true, { available: true, reason: "" });
    let sent: { options?: Record<string, unknown> } | undefined;
    server.use(
      http.post("/api/subtitles/batch", async ({ request }) => {
        sent = (await request.json()) as typeof sent;
        return HttpResponse.json({
          queued: 1,
          skipped: 0,
          errors: [],
          job_id: 5,
        });
      }),
    );
    customRender(<MassTranslateForm items={[MOVIE]} />);
    await languagesLoaded();
    await screen.findByRole("combobox", { name: "Translate with" });
    await stateLoaded();

    await choose(user, "Translate with", "OpenAI compatible, usable-model");
    expect(
      screen.getByText(/OpenAI compatible, usable-model will be used/),
    ).toBeInTheDocument();
    await choose(user, "Source Language", "English");
    await choose(user, "Target Language", "Hungarian");
    await user.click(
      screen.getByRole("button", { name: "Translate 1 Item(s)" }),
    );

    await waitFor(() => expect(sent).toBeDefined());
    expect(sent?.options).toMatchObject({
      from_lang: "en",
      to_lang: "hu",
      connection_id: 1,
    });
  });

  it("sends the picked engine in the batch options when it has no back ends", async () => {
    const user = userEvent.setup();
    settings();
    translatorStatus(true, { available: true, reason: "" });
    let sent: { options?: Record<string, unknown> } | undefined;
    server.use(
      http.post("/api/subtitles/batch", async ({ request }) => {
        sent = (await request.json()) as typeof sent;
        return HttpResponse.json({
          queued: 1,
          skipped: 0,
          errors: [],
          job_id: 5,
        });
      }),
    );
    customRender(<MassTranslateForm items={[MOVIE]} />);
    await languagesLoaded();
    await screen.findByRole("combobox", { name: "Translate with" });
    await stateLoaded();

    await choose(user, "Translate with", "Google Translate");
    await choose(user, "Source Language", "English");
    await choose(user, "Target Language", "Hungarian");
    await user.click(
      screen.getByRole("button", { name: "Translate 1 Item(s)" }),
    );

    await waitFor(() => expect(sent).toBeDefined());
    expect(sent?.options).toMatchObject({ engine_id: 2 });
    expect(sent?.options).not.toHaveProperty("connection_id");
  });

  it.each([false, true])(
    "sends no manual pick for Automatic (engine removed: %s)",
    async (removed) => {
      const user = userEvent.setup();
      settings();
      translatorStatus(true, { available: true, reason: "" });
      let sent: { options?: Record<string, unknown> } | undefined;
      server.use(
        http.post("/api/subtitles/batch", async ({ request }) => {
          sent = (await request.json()) as typeof sent;
          return HttpResponse.json({
            queued: 1,
            skipped: 0,
            errors: [],
            job_id: 5,
          });
        }),
      );
      customRender(<MassTranslateForm items={[MOVIE]} />);
      await languagesLoaded();
      const picker = await screen.findByRole("combobox", {
        name: "Translate with",
      });
      if (removed) {
        await stateLoaded();
        await choose(user, "Translate with", "Google Translate");
        await refreshPickInstallations(
          PICK_INSTALLATIONS.filter(
            (item) => item.provider_id !== "google_translate",
          ),
        );
        await waitFor(() => expect(picker).toHaveValue("Automatic"));
      }

      await choose(user, "Source Language", "English");
      await choose(user, "Target Language", "Hungarian");
      await user.click(
        screen.getByRole("button", { name: "Translate 1 Item(s)" }),
      );

      await waitFor(() => expect(sent).toBeDefined());
      expect(sent?.options).not.toHaveProperty("connection_id");
      expect(sent?.options).not.toHaveProperty("engine_id");
    },
  );

  it("lets a picked back end start when the selected engine's gate is closed", async () => {
    const user = userEvent.setup();
    settings();
    translatorStatus(true, {
      available: false,
      reason: "No translator is configured",
    });
    customRender(<MassTranslateForm items={[MOVIE]} />);
    await screen.findByText("No translator is configured");
    expect(
      screen.getByRole("button", { name: "Translate 1 Item(s)" }),
    ).toBeDisabled();
    await stateLoaded();

    await choose(user, "Translate with", "OpenAI compatible, usable-model");

    expect(
      screen.getByRole("button", { name: "Translate 1 Item(s)" }),
    ).toBeEnabled();
  });

  it("shows no picker and never asks for the page's state while the new translator is off", async () => {
    settings();
    const asked = translatorStatus(false, { available: true, reason: "" });
    customRender(<MassTranslateForm items={[MOVIE]} />);

    expect(
      await screen.findByText(/Google Translate will be used/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "Translate with" }),
    ).not.toBeInTheDocument();
    expect(asked).toEqual([]);
  });
});
