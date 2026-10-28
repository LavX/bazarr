/* eslint-disable camelcase -- API fixtures use the server's field names. */
/**
 * The translate form reads the translation gate from the system status. A
 * closed gate disables the action and shows the gate's reason, and no engine
 * the form does not recognise is labelled "Google Translate".
 */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import TranslationForm from "@/components/forms/TranslationForm";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const LANGUAGES = [
  { code2: "en", code3: "eng", name: "English", enabled: true },
  { code2: "hu", code3: "hun", name: "Hungarian", enabled: true },
];

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

describe("TranslationForm and the translation gate", () => {
  it("disables the action and shows the reason when the gate is closed", async () => {
    configure("none", {
      available: false,
      reason: "No translator is configured",
    });
    customRender(<TranslationForm selections={[]} />);

    expect(
      await screen.findByText("No translator is configured"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  });

  it("does not label an engine it does not recognise Google Translate", async () => {
    configure("weird_engine", {
      available: false,
      reason: "Unknown translator type",
    });
    customRender(<TranslationForm selections={[]} />);

    expect(
      await screen.findByText("Unknown translator type"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Google Translate/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  });

  it("keeps the action enabled and names the engine when the gate is open", async () => {
    configure("gemini", { available: true, reason: "" });
    customRender(<TranslationForm selections={[]} />);

    expect(await screen.findByText(/Gemini/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeEnabled();
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
    configure("gemini", { available: true, reason: "" });
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
      // The subtitles endpoint answers the gate's refusal with a bare
      // sentence and 409, which is the body the refusal reads. The
      // sentence differs from the status answer's so the toast is caught
      // reading the 409's own body, not the refetched status.
      http.patch(
        "/api/subtitles",
        () => new HttpResponse("Translation was switched off", { status: 409 }),
      ),
    );
    customRender(
      <TranslationForm
        selections={
          [
            {
              id: 5,
              type: "movie",
              language: "en",
              path: "/movies/m.en.srt",
              hi: "False",
              forced: "False",
              arr_instance_id: 2,
            },
          ] as FormType.ModifySubtitle[]
        }
      />,
    );

    // The combobox has no options until the languages query settles, so the
    // interaction waits for it instead of clicking an empty dropdown.
    await waitFor(() =>
      expect(
        queryClient.getQueryState([
          QueryKeys.System,
          QueryKeys.Languages,
          false,
        ])?.status,
      ).toBe("success"),
    );
    await user.click(screen.getByRole("combobox"));
    // The dropdown's options are outside the accessibility tree jsdom
    // computes, so they are matched with hidden: true once they exist.
    await user.click(
      await screen.findByRole("option", { name: "Hungarian", hidden: true }),
    );
    await user.click(screen.getByRole("button", { name: "Start" }));

    // The 409's own reason, which only the toast can show: the gate alert
    // reads the status answer's reason, a different sentence. The generic
    // sentence the catch used to replace it with is gone.
    expect(
      await screen.findByText("Translation was switched off"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("An error occurred while translating subtitles"),
    ).not.toBeInTheDocument();

    // The refusal also refetches the status, so the action reads the closed
    // gate and disables itself.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Start" })).toBeDisabled(),
    );
  });

  it("sends the source row's codec with the request, chosen by typing the language", async () => {
    const user = userEvent.setup();
    configure("gemini", { available: true, reason: "" });
    let sent: Record<string, FormDataEntryValue> | undefined;
    server.use(
      http.patch("/api/subtitles", async ({ request }) => {
        sent = Object.fromEntries((await request.formData()).entries());
        return HttpResponse.json({ job_id: 3 }, { status: 202 });
      }),
    );
    customRender(
      <TranslationForm
        selections={
          [
            {
              id: 5,
              type: "movie",
              language: "en",
              path: "",
              hi: "False",
              forced: "False",
              from_language: "en",
              codec: "subrip",
              arr_instance_id: 2,
            },
          ] as FormType.ModifySubtitle[]
        }
      />,
    );

    await waitFor(() =>
      expect(
        queryClient.getQueryState([
          QueryKeys.System,
          QueryKeys.Languages,
          false,
        ])?.status,
      ).toBe("success"),
    );
    await user.type(screen.getByRole("combobox"), "Hung");
    await user.click(
      await screen.findByRole("option", { name: "Hungarian", hidden: true }),
    );
    await user.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(sent).toBeDefined());
    expect(sent).toMatchObject({
      language: "hu",
      path: "",
      from_language: "en",
      codec: "subrip",
    });
  });
});
