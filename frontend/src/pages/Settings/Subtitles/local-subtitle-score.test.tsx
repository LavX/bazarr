/* eslint-disable camelcase */

import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import SettingsSubtitlesView from "@/pages/Settings/Subtitles";
import { customRender, screen } from "@/tests";
import server from "@/tests/mocks/node";

describe("Subtitles settings, local subtitle score", () => {
  beforeEach(() => {
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { theme: "auto", use_embedded_subs: true },
          ocr: { tessdata_dir: "" },
        }),
      ),
      http.post(
        "/api/system/settings",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );
  });

  it("labels the local subtitle score input", () => {
    customRender(<SettingsSubtitlesView />);

    expect(
      screen.getByRole("textbox", { name: "Local subtitle score (%)" }),
    ).toBeInTheDocument();
  });
});
