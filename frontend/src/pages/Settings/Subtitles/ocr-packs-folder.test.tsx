/* eslint-disable camelcase */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import SettingsSubtitlesView from "@/pages/Settings/Subtitles";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

describe("Subtitles settings, OCR language packs folder", () => {
  let saved: FormData | undefined;

  beforeEach(() => {
    saved = undefined;
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { theme: "auto", use_embedded_subs: true },
          ocr: { tessdata_dir: "" },
        }),
      ),
      http.post("/api/system/settings", async ({ request }) => {
        saved = await request.formData();
        return new HttpResponse(null, { status: 204 });
      }),
    );
  });

  it("sits with the embedded subtitles options and saves the folder", async () => {
    const user = userEvent.setup();
    customRender(<SettingsSubtitlesView />);

    const folder = await screen.findByRole("textbox", {
      name: "OCR Language Packs Folder",
    });
    expect(folder).toHaveValue("");
    expect(folder).toHaveAttribute(
      "placeholder",
      "tessdata in the config folder",
    );

    await user.type(folder, "/srv/tessdata");
    await user.click(
      await screen.findByRole("button", { name: /Save 1 pending change/ }),
    );

    await waitFor(() =>
      expect(saved?.get("settings-ocr-tessdata_dir")).toBe("/srv/tessdata"),
    );
  });
});
