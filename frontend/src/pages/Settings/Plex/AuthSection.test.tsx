/* eslint-disable camelcase */

import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { customRender, screen, waitFor, within } from "@/tests";
import AuthSection from "./AuthSection";

// Every case below drives the same button. What changes is what the pin
// mutation does, because each failure used to reach `pin.authUrl` and throw an
// unhandled rejection that left the button looking inert.
const createPin = vi.fn();
const notify = vi.fn();
const logout = vi.fn();
let auth: Record<string, unknown> = { valid: false, auth_method: null };

// Only `notifications.show` is replaced. The module also exports the
// <Notifications /> component that the shared test providers render, so a bare
// factory here blanks it and every case fails inside providers.tsx instead.
vi.mock("@mantine/notifications", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@mantine/notifications")>()),
  notifications: { show: (args: unknown) => notify(args) },
}));

vi.mock("@/apis/hooks/plex", () => ({
  usePlexAuthValidationQuery: () => ({
    data: auth,
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  }),
  usePlexPinMutation: () => ({ mutateAsync: createPin }),
  usePlexLogoutMutation: () => ({ mutate: logout, isPending: false }),
  usePlexPinCheckQuery: () => ({ data: undefined }),
}));

const startAuth = async () => {
  const user = userEvent.setup();
  customRender(<AuthSection />);
  await user.click(screen.getByRole("button", { name: /connect to plex/i }));
};

describe("Plex AuthSection", () => {
  beforeEach(() => {
    createPin.mockReset();
    notify.mockReset();
    logout.mockReset();
    auth = { valid: false, auth_method: null };
    vi.spyOn(window, "open").mockReturnValue({} as Window);
  });

  it("explains a Plex request that never answered instead of throwing", async () => {
    createPin.mockRejectedValue(new Error("connect ECONNREFUSED"));

    await startAuth();

    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Could not reach Plex",
          message: "connect ECONNREFUSED",
          color: "red",
        }),
      ),
    );
    expect(window.open).not.toHaveBeenCalled();
  });

  it("explains a response that carries no sign-in link", async () => {
    // The exact shape that produced "Cannot read properties of null
    // (reading 'authUrl')".
    createPin.mockResolvedValue({ data: null });

    await startAuth();

    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Could not start Plex sign-in",
          color: "red",
        }),
      ),
    );
    expect(window.open).not.toHaveBeenCalled();
  });

  it("tells the user when the sign-in window was blocked, and stops polling", async () => {
    createPin.mockResolvedValue({
      data: { pinId: 1, code: "ABCD", authUrl: "https://plex.tv/link" },
    });
    vi.spyOn(window, "open").mockReturnValue(null);

    await startAuth();

    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Plex sign-in window was blocked",
          color: "red",
        }),
      ),
    );
    // A blocked popup must not leave a pin behind, or the panel polls for a
    // sign-in that cannot happen.
    expect(
      screen.queryByText(/ABCD/, { exact: false }),
    ).not.toBeInTheDocument();
  });

  it("opens the Plex window when a pin carries a sign-in link", async () => {
    createPin.mockResolvedValue({
      data: { pinId: 1, code: "ABCD", authUrl: "https://plex.tv/link" },
    });

    await startAuth();

    await waitFor(() =>
      expect(window.open).toHaveBeenCalledWith(
        "https://plex.tv/link",
        "PlexAuth",
        expect.any(String),
      ),
    );
    expect(notify).not.toHaveBeenCalled();
  });

  it("asks before signing out of Plex, and only Disconnect signs out", async () => {
    // Signing out turns use_plex off, which stops refreshes to every Plex
    // server, the hand-added ones too. This button ran it on the first
    // click, while the account card's Disconnect already asked.
    auth = {
      valid: true,
      auth_method: "oauth",
      username: "reader",
      email: "reader@example.com",
    };
    const user = userEvent.setup();
    customRender(<AuthSection />);
    const signOut = screen.getByRole("button", {
      name: "Disconnect from Plex",
    });

    await user.click(signOut);
    const dialog = await screen.findByRole("dialog", {
      name: "Disconnect from Plex",
    });
    expect(
      within(dialog).getByText(
        "This signs you out of Plex and turns off Plex integration, which also stops refreshes to any Plex server you added by hand.",
      ),
    ).toBeInTheDocument();
    expect(logout).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Disconnect from Plex" }),
      ).not.toBeInTheDocument(),
    );
    expect(logout).not.toHaveBeenCalled();

    await user.click(signOut);
    const again = await screen.findByRole("dialog", {
      name: "Disconnect from Plex",
    });
    await user.click(within(again).getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(logout).toHaveBeenCalledTimes(1));
  });
});
