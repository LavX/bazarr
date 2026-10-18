/* eslint-disable camelcase -- the status fixture uses the server's field name. */
/**
 * The profile editor warns, above the languages table, when a profile can
 * translate from another language but no translator is available: those
 * languages fall back to the provider search, and the editor is the place a
 * user decides whether that is what they want.
 */
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import ProfileEditForm from "@/components/forms/ProfileEditForm";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const LANGUAGES = [
  { code2: "en", name: "English" },
  { code2: "hu", name: "Hungarian" },
];

const PROFILE: Language.Profile = {
  name: "Example",
  profileId: 1,
  cutoff: null,
  items: [
    {
      id: 1,
      audio_exclude: "False",
      audio_only_include: "False",
      forced: "False",
      hi: "False",
      language: "hu",
      translate_from: "en",
    },
  ],
  mustContain: [],
  mustNotContain: [],
  originalFormat: null,
  tag: undefined,
};

function renderForm(availability: { available: boolean; reason: string }) {
  server.use(
    http.get("/api/system/status", () =>
      HttpResponse.json({
        data: { translation_availability: availability },
      }),
    ),
  );
  customRender(<ProfileEditForm languages={LANGUAGES} profile={PROFILE} />);
}

async function waitForTheStatusToSettle() {
  await waitFor(() =>
    expect(
      queryClient.getQueryState([QueryKeys.System, "status"])?.status,
    ).toBe("success"),
  );
}

describe("ProfileEditForm and the translation gate", () => {
  it("warns above the table when no translator is available", async () => {
    renderForm({ available: false, reason: "No translator is configured" });

    expect(
      await screen.findByText(
        /Translation is unavailable: No translator is configured/,
      ),
    ).toBeInTheDocument();
  });

  it("says nothing while a translator is available", async () => {
    renderForm({ available: true, reason: "" });

    await waitForTheStatusToSettle();
    expect(
      screen.queryByText(/Translation is unavailable/),
    ).not.toBeInTheDocument();
  });
});
