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

const smokeManifest = {
  ...manifest,
  provider_id: "smokehub",
  name: "SmokeHub",
  description: "Deterministic Provider Hub smoke provider.",
  version: "0.2.0",
  config_schema: {
    type: "object",
    properties: {
      profile_name: {
        type: "string",
        title: "Profile name",
      },
      api_token: {
        type: "string",
        title: "API token",
        secret: true,
      },
    },
  },
  secret_fields: ["api_token"],
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
    const restartRequest = vi.fn();
    sessionStorage.clear();
    server.use(
      http.post("/api/system", ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("action") === "restart") {
          restartRequest();
        }
        return new HttpResponse(null, { status: 204 });
      }),
    );

    customRender(<SettingsProvidersView />);

    await waitFor(() => {
      expect(
        screen.getByText(/Restart Bazarr\+ to activate/i),
      ).toBeInTheDocument();
    });

    await userEvent.click(
      screen.getByRole("button", { name: /Restart Bazarr now/i }),
    );

    await waitFor(() => expect(restartRequest).toHaveBeenCalledTimes(1));
    expect(
      sessionStorage.getItem("bazarr.restart.reload_after_reconnect"),
    ).toBe("1");
  });

  it("shows installed hub plugins separately and can uninstall them", async () => {
    const uninstallRequest = vi.fn();
    server.use(
      http.post("/api/provider-hub/providers/officialhub/test", () => {
        return HttpResponse.json({
          provider_id: "officialhub",
          ok: true,
          status: "ready",
          message: "Worker health check passed",
        });
      }),
      http.delete("/api/provider-hub/installations/officialhub", () => {
        uninstallRequest();
        return new HttpResponse(null, { status: 204 });
      }),
    );

    customRender(<SettingsProvidersView />);

    const panel = await screen.findByRole("tabpanel", {
      name: /My Providers/i,
    });

    await screen.findByText(/Restart Bazarr\+ to activate/i);

    expect(
      within(panel).getByText("Enabled search providers"),
    ).toBeInTheDocument();
    expect(within(panel).getByText("Installed plugins")).toBeInTheDocument();
    expect(
      within(panel).getByText("Official Hub Provider"),
    ).toBeInTheDocument();

    await userEvent.click(within(panel).getByLabelText("Provider actions"));
    await userEvent.click(await screen.findByText("Test connection"));

    await screen.findByText("Worker health check passed");

    await userEvent.click(within(panel).getByLabelText("Provider actions"));
    await userEvent.click(await screen.findByText("Uninstall"));

    await waitFor(() => expect(uninstallRequest).toHaveBeenCalledTimes(1));
  });

  it("offers active hub plugins in the same add-provider flow as shipped providers", async () => {
    server.use(
      http.get("/api/provider-hub/providers", () => {
        return HttpResponse.json({
          data: [
            {
              provider_id: "smokehub",
              name: "SmokeHub",
              active_version: "0.2.0",
              staged_version: null,
              state: "active",
              pending_restart: false,
              trusted: true,
              active_path: "/config/provider_hub/bundles/smokehub",
              python_path: "/config/provider_hub/envs/smokehub/bin/python",
              last_error: null,
              manifest: smokeManifest,
            },
          ],
        });
      }),
    );

    customRender(<SettingsProvidersView />);

    const panel = await screen.findByRole("tabpanel", {
      name: /My Providers/i,
    });

    await userEvent.click(
      await within(panel).findByRole("button", {
        name: /Add search provider/i,
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: /Provider settings/i,
    });
    await userEvent.click(
      within(dialog).getByPlaceholderText("Click to Select a Provider"),
    );
    const [smokeOption] = await screen.findAllByText("SmokeHub");
    await userEvent.click(smokeOption);

    expect(screen.getByLabelText("Profile name")).toBeInTheDocument();
    expect(screen.getByLabelText("API token")).toBeInTheDocument();
    expect(
      screen.getByText(/Provider Hub plugin is installed but not enabled/i),
    ).toBeInTheDocument();
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
