/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SettingsPanel from "@/pages/DistributionHub/SettingsPanel";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const baseSettings = {
  enabled: true,
  consent: false,
  search_timeout_seconds: 20,
  search_rate_limit_enabled: true,
  usage_retention_days: 400,
  default_tier: "free",
  downloads_per_window: 0,
  downloads_window_seconds: 0,
  serve_local_subs: true,
  has_token: true,
};

describe("DistributionHub > SettingsPanel consent", () => {
  beforeEach(() => {
    server.use(
      http.get("/api/distribution-hub/settings", () =>
        HttpResponse.json(baseSettings),
      ),
    );
  });

  it("renders the consent switch and includes it when saving", async () => {
    const user = userEvent.setup();
    const patched = vi.fn();
    server.use(
      http.patch("/api/distribution-hub/settings", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        patched(body);
        return HttpResponse.json({ ...baseSettings, ...body });
      }),
    );

    customRender(<SettingsPanel />);

    const consent = await screen.findByLabelText(/must not be exposed/i);
    expect(consent).toBeInTheDocument();
    await user.click(consent);
    await user.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() => expect(patched).toHaveBeenCalled());
    expect(patched.mock.calls[0][0]).toMatchObject({ consent: true });
  });
});
