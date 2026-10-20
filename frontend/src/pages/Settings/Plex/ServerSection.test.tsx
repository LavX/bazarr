/* eslint-disable camelcase */

/**
 * Tests for the Plex server card's selection refusal messages.
 *
 * Why: the picker refuses an address Plex does not list for the signed-in
 * account, and refuses everything while plex.tv cannot be asked. Those
 * refusals have to be said on the card, not swallowed by the mutation.
 *
 * What: renders ServerSection with the plex hooks mocked, one available
 * server so the automatic single-server selection runs, and a selection
 * mutation that rejects with each refusal code.
 *
 * Test: Run with `cd frontend && npx vitest run --reporter=verbose`.
 */

import { AxiosError } from "axios";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ServerSection from "@/pages/Settings/Plex/ServerSection";
import { customRender, fireEvent, screen, waitFor } from "@/tests";

const mockSelect = vi.fn();

vi.mock("@/apis/hooks/plex", async (importActual) => {
  const actual = await importActual<typeof import("@/apis/hooks/plex")>();
  return {
    ...actual,
    usePlexAuthValidationQuery: () => ({
      data: { valid: true, auth_method: "oauth" },
    }),
    usePlexServersQuery: () => ({
      data: [server()],
      error: undefined,
      refetch: vi.fn(),
    }),
    usePlexSelectedServerQuery: () => ({ data: undefined }),
    usePlexServerSelectionMutation: () => ({ mutateAsync: mockSelect }),
  };
});

vi.mock("./ConnectionsCard", () => ({ default: () => null }));

function server(): Plex.Server {
  return {
    name: "Attic",
    machineIdentifier: "machine-abc",
    connections: [
      {
        uri: "https://listed.example:32400",
        protocol: "https",
        address: "listed.example",
        port: 32400,
        local: true,
      },
    ],
    version: "1.40.0",
    platform: "Linux",
    device: "PC",
    bestConnection: {
      uri: "https://listed.example:32400",
      protocol: "https",
      address: "listed.example",
      port: 32400,
      local: true,
    },
  };
}

function refusal(errorCode: string, message: string) {
  return new AxiosError(
    "Request failed",
    "ERR_BAD_REQUEST",
    undefined,
    undefined,
    { status: 400, data: { error_code: errorCode, error: message } } as never,
  );
}

beforeEach(() => {
  mockSelect.mockReset();
});

describe("ServerSection, a refused selection", () => {
  it("says Plex does not list the address for the selected server", async () => {
    mockSelect.mockRejectedValue(
      refusal(
        "server_not_listed",
        "Plex does not list this address for the selected server",
      ),
    );
    customRender(<ServerSection />);

    await waitFor(() => {
      expect(
        screen.getByText(/Plex does not list this address/i),
      ).toBeInTheDocument();
    });
    expect(mockSelect).toHaveBeenCalledTimes(1);
  });

  it("says Plex could not be reached while plex.tv does not answer", async () => {
    mockSelect.mockRejectedValue(
      refusal(
        "plex_unreachable",
        "Plex could not be reached to verify the server address",
      ),
    );
    customRender(<ServerSection />);

    await waitFor(() => {
      expect(
        screen.getByText(/Plex could not be reached/i),
      ).toBeInTheDocument();
    });
  });

  it("passes any other refusal on in the backend's own words", async () => {
    // account_changed asks the reader to choose the server again; the generic
    // line hid that once the client's toast no longer carried it.
    mockSelect.mockRejectedValue(
      refusal(
        "account_changed",
        "The Plex account changed. Choose the server again.",
      ),
    );
    customRender(<ServerSection />);

    await waitFor(() => {
      expect(
        screen.getByText("The Plex account changed. Choose the server again."),
      ).toBeInTheDocument();
    });
  });

  it("keeps the generic refusal text for anything else", async () => {
    mockSelect.mockRejectedValue(new Error("network down"));
    customRender(<ServerSection />);

    await waitFor(() => {
      expect(
        screen.getByText(/Could not save the selected server/i),
      ).toBeInTheDocument();
    });
  });
});

describe("ServerSection, a failed automatic selection", () => {
  it("recovers from plex_unreachable through a retry action", async () => {
    mockSelect
      .mockRejectedValueOnce(
        refusal(
          "plex_unreachable",
          "Plex could not be reached to verify the server address",
        ),
      )
      .mockResolvedValueOnce(undefined);
    customRender(<ServerSection />);

    await waitFor(() => {
      expect(
        screen.getByText(/Plex could not be reached/i),
      ).toBeInTheDocument();
    });

    const retry = screen.getByRole("button", { name: /try again/i });
    fireEvent.click(retry);

    await waitFor(() => {
      expect(screen.getByText("Connected")).toBeInTheDocument();
    });
    expect(mockSelect).toHaveBeenCalledTimes(2);
    expect(
      screen.queryByText(/Plex could not be reached/i),
    ).not.toBeInTheDocument();
  });
});
