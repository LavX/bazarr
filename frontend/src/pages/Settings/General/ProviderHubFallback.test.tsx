import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import GeneralSettings from "@/pages/Settings/General";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

it("offers the catalog mirror setting and saves it under its own key", async () => {
  const submitted: FormData[] = [];
  server.use(
    http.post("/api/system/settings", async ({ request }) => {
      submitted.push(await request.formData());
      return new HttpResponse(null, { status: 204 });
    }),
  );
  customRender(<GeneralSettings />);
  const user = userEvent.setup();

  const box = await screen.findByRole("switch", {
    name: /Read the catalog from the GitHub Pages mirror when GitHub refuses/,
  });
  await user.click(box);
  await user.click(
    await screen.findByRole("button", { name: /Save 1 pending/ }),
  );

  await waitFor(() => expect(submitted).toHaveLength(1));
  expect(submitted[0].has("settings-general-provider_hub_allow_fallback")).toBe(
    true,
  );
});
