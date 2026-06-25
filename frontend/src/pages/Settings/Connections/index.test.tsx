import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import SettingsConnectionsView from "./index";

describe("Connections page", () => {
  it("renders a tab for each service", async () => {
    customRender(<SettingsConnectionsView />);
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /sonarr/i })).toBeInTheDocument();
    });
    expect(screen.getByRole("tab", { name: /radarr/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /plex/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /jellyfin/i })).toBeInTheDocument();
  });

  it("shows Sonarr global options on the default tab (lossless fold)", async () => {
    // Enable Sonarr so the CollapseBox is open and its children are in the DOM.
    // Mantine Collapse with transitionDuration={0} returns null (not mounted)
    // when expanded=false, so this override is required to reach the gated options.
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { theme: "auto", use_sonarr: true } }),
      ),
    );
    customRender(<SettingsConnectionsView />);
    // Master toggle relocated from the old Sonarr page.
    await waitFor(() => {
      expect(screen.getByText("Use Sonarr")).toBeInTheDocument();
    });
    // A representative global option that is NOT part of the instance card form.
    await waitFor(() => {
      expect(screen.getByText("Download Only Monitored")).toBeInTheDocument();
    });
  });

  it("switches to the Plex tab and shows its config", async () => {
    const user = userEvent.setup();
    customRender(<SettingsConnectionsView />);
    await user.click(await screen.findByRole("tab", { name: /plex/i }));
    await waitFor(() => {
      expect(screen.getByText("Use Plex Media Server")).toBeInTheDocument();
    });
  });
});
