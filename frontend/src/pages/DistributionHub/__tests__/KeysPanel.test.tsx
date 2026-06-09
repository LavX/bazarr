/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import KeysPanel from "@/pages/DistributionHub/KeysPanel";
import { customRender, screen } from "@/tests";
import server from "@/tests/mocks/node";

const legacyKey = {
  id: 1,
  name: "Default",
  key_prefix: "ab12",
  tier: "unlimited",
  tier_label: "Unlimited",
  enabled: 1,
  is_legacy: 1,
  timeout_seconds: null,
  custom_limits: null,
  excluded_providers: null,
  allowed_providers: null,
  created_at: null,
  last_used_at: null,
  note: null,
  // usage/limits omitted on purpose: this test only covers the rotate flow,
  // and UsageCell renders a dash when they are absent.
};

describe("DistributionHub > KeysPanel legacy rotation", () => {
  beforeEach(() => {
    server.use(
      http.get("/api/distribution-hub/keys", () =>
        HttpResponse.json({ keys: [legacyKey], default_tier: "unlimited" }),
      ),
      http.get("/api/distribution-hub/tiers", () =>
        HttpResponse.json({ default_tier: "unlimited", tiers: {} }),
      ),
      http.get("/api/distribution-hub/providers", () =>
        HttpResponse.json({ providers: [] }),
      ),
      http.post("/api/distribution-hub/regenerate", () =>
        HttpResponse.json({ ok: true, token: "newtok123" }),
      ),
      http.get("/api/distribution-hub/legacy-token", () =>
        HttpResponse.json({ token: "legacytok456" }),
      ),
    );
  });

  it("rotates the legacy key inline and reveals the new token", async () => {
    const user = userEvent.setup();
    customRender(<KeysPanel />);

    expect(await screen.findByText("Default")).toBeInTheDocument();
    expect(screen.getByText("legacy")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /key actions/i }));
    await user.click(
      await screen.findByRole("menuitem", { name: /rotate token/i }),
    );
    await user.click(await screen.findByRole("button", { name: /^rotate$/i }));

    expect(await screen.findByText("newtok123")).toBeInTheDocument();
  });

  it("reveals the legacy token inline without rotating", async () => {
    const user = userEvent.setup();
    customRender(<KeysPanel />);

    expect(await screen.findByText("Default")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /key actions/i }));
    await user.click(
      await screen.findByRole("menuitem", { name: /reveal token/i }),
    );
    // The stored shared token is shown as-is (re-viewable, not a one-time secret).
    expect(await screen.findByText("legacytok456")).toBeInTheDocument();
  });
});
