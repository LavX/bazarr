/* eslint-disable camelcase */

import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SettingsProvidersView from "@/pages/Settings/Providers";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

const manifest = {
  schema_version: 1,
  provider_id: "officialhub",
  name: "Official Hub Provider",
  version: "1.0.0",
  source: {
    type: "github",
    repo: "LavX/bazarr-provider-catalog",
    ref: "main",
    commit: "a".repeat(40),
    trusted: true,
  },
};

describe("Settings > Providers (Provider Hub)", () => {
  beforeEach(() => {
    server.use(
      http.get("/api/system/settings", () => {
        return HttpResponse.json({});
      }),
      http.get("/api/provider-hub/catalog", () => {
        return HttpResponse.json({
          sources: [
            {
              id: "official",
              name: "Official",
              type: "github",
              url: "https://github.com/bazarr/provider-hub/blob/main/catalog.json",
              enabled: true,
              trusted: true,
              last_error: null,
            },
          ],
          entries: [
            {
              source: "Official",
              provider_id: "officialhub",
              name: "Official Hub Provider",
              version: "1.0.0",
              trusted: true,
              manifest,
            },
          ],
        });
      }),
      http.get("/api/provider-hub/providers", () => {
        return HttpResponse.json({
          data: [
            {
              provider_id: "officialhub",
              name: "Official Hub Provider",
              active_version: null,
              staged_version: "1.0.0",
              state: "staged",
              pending_restart: true,
              trusted: true,
              staged_path: "/config/provider_hub/staged/officialhub",
              staged_python_path:
                "/config/provider_hub/venvs/officialhub/bin/python",
              last_error: "Restart required before activation",
              manifest,
            },
          ],
        });
      }),
      http.get("/api/provider-hub/jobs", () => {
        return HttpResponse.json({ data: [] });
      }),
    );
  });

  it("surfaces the restart-required banner when a hub provider is staged", async () => {
    customRender(<SettingsProvidersView />);

    await waitFor(() => {
      expect(
        screen.getByText(/Restart Bazarr\+ to activate/i),
      ).toBeInTheDocument();
    });
  });

  it("lists installed hub providers on My Providers", async () => {
    customRender(<SettingsProvidersView />);

    const panel = await screen.findByRole("tabpanel", {
      name: /My Providers/i,
    });

    expect(
      await within(panel).findByText("Installed hub providers"),
    ).toBeInTheDocument();
    expect(
      within(panel).getByText("Official Hub Provider"),
    ).toBeInTheDocument();
    expect(within(panel).getByText("Restart required")).toBeInTheDocument();
  });

  it("preserves the trusted-source attribution when installing from catalog", async () => {
    const installRequest = vi.fn();
    server.use(
      http.post("/api/provider-hub/installations", async ({ request }) => {
        installRequest(await request.json());
        return HttpResponse.json({
          provider_id: "officialhub",
          name: "Official Hub Provider",
          active_version: null,
          staged_version: "1.0.0",
          state: "staged",
          pending_restart: true,
          trusted: true,
          manifest,
        });
      }),
      // When there are no installed providers, the catalog entry's CTA reads "Install".
      http.get("/api/provider-hub/providers", () => {
        return HttpResponse.json({ data: [] });
      }),
    );

    customRender(<SettingsProvidersView />);

    await userEvent.click(screen.getByRole("tab", { name: /Marketplace/i }));
    const panel = await screen.findByRole("tabpanel", {
      name: /Marketplace/i,
    });
    await within(panel).findByText("Official Hub Provider");

    await userEvent.click(
      within(panel).getByRole("button", { name: /^Install$/i }),
    );

    await waitFor(() => {
      expect(installRequest).toHaveBeenCalledWith({
        manifest: expect.objectContaining({
          provider_id: "officialhub",
          source: expect.objectContaining({
            repo: "LavX/bazarr-provider-catalog",
            trusted: true,
          }),
        }),
      });
    });
  });
});
