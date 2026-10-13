/* eslint-disable camelcase */

/**
 * Tests for the media server instance edit modal's destination rule.
 *
 * Why: a stored credential is only allowed to travel to the address it was
 * saved with, so an edit that moves the destination has to collect the key
 * again rather than rely on a silent server-side refusal.
 *
 * What: renders the modal with a stored-key instance and drives it with
 * @testing-library/user-event, typing into the fields (never pasting), and
 * asserts the Keep mode switches itself off with a reason, and that a server
 * refusal message is shown rather than generic retry text.
 *
 * Test: Run with `cd frontend && npx vitest run --reporter=verbose`.
 */

import userEvent from "@testing-library/user-event";
import { AxiosError } from "axios";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MediaServerInstance,
  MediaServerKind,
} from "@/apis/raw/mediaServers";
import MediaServerInstanceFormModal from "@/pages/Settings/MediaServers/MediaServerInstanceFormModal";
import { customRender, screen, waitFor } from "@/tests";

const mockSaveMutate = vi.fn();
const mockTestMutate = vi.fn();
const mockLibrariesMutate = vi.fn();
let mockSaveError: unknown = undefined;

vi.mock("@/apis/hooks/mediaServers", async (importActual) => {
  const actual =
    await importActual<typeof import("@/apis/hooks/mediaServers")>();
  return {
    ...actual,
    useSaveMediaServerInstance: () => ({
      mutate: mockSaveMutate,
      isPending: false,
      isError: mockSaveError !== undefined,
      error: mockSaveError,
    }),
    useMediaServerTest: () => ({
      mutate: mockTestMutate,
      isPending: false,
      isIdle: true,
      isError: false,
      isSuccess: false,
      data: undefined,
      reset: vi.fn(),
    }),
    useMediaServerLibraries: () => ({
      mutate: mockLibrariesMutate,
      isPending: false,
      isIdle: true,
      isError: false,
      isSuccess: false,
      data: undefined,
      reset: vi.fn(),
    }),
  };
});

function makeInstance(
  overrides: Partial<MediaServerInstance> = {},
): MediaServerInstance {
  return {
    id: "2af88684-d7d2-4534-82bb-a6d839cc5c10",
    kind: "emby",
    name: "Living room",
    enabled: true,
    url: "https://emby.example",
    verify_ssl: true,
    api_key_set: true,
    path_mappings: [{ local_path: "/movies", remote_path: "/media/movies" }],
    refresh_movies: true,
    refresh_episodes: true,
    options: {},
    ...overrides,
  };
}

function renderModal(
  instance: MediaServerInstance | null,
  kind: MediaServerKind = "emby",
) {
  customRender(
    <MediaServerInstanceFormModal
      kind={kind}
      instance={instance}
      onClose={vi.fn()}
    />,
  );
}

beforeEach(() => {
  mockSaveMutate.mockClear();
  mockTestMutate.mockClear();
  mockLibrariesMutate.mockClear();
  mockSaveError = undefined;
});

describe("MediaServerInstanceFormModal, destination change", () => {
  it("switches Keep off and explains why when the Server URL is typed over", async () => {
    const user = userEvent.setup();
    renderModal(makeInstance());

    const url = screen.getByLabelText("Server URL");
    await user.clear(url);
    await user.type(url, "https://moved.example");

    await waitFor(() => {
      expect(screen.getByLabelText("API Key")).toBeInTheDocument();
    });
    expect(
      screen.getByText(/stored api key cannot be kept/i),
    ).toBeInTheDocument();
  });

  it("switches Keep off when certificate verification is switched off", async () => {
    const user = userEvent.setup();
    renderModal(makeInstance());

    await user.click(screen.getByRole("switch", { name: /verify ssl/i }));

    await waitFor(() => {
      expect(screen.getByLabelText("API Key")).toBeInTheDocument();
    });
    expect(
      screen.getByText(/stored api key cannot be kept/i),
    ).toBeInTheDocument();
  });

  it("keeps the stored key when certificate verification is switched on", async () => {
    const user = userEvent.setup();
    // Verification is off, so the only change is turning it on, which
    // hardens the connection rather than moving it.
    renderModal(makeInstance({ verify_ssl: false }));

    await user.click(screen.getByRole("switch", { name: /verify ssl/i }));

    expect(screen.queryByLabelText("API Key")).not.toBeInTheDocument();
    expect(
      screen.queryByText(/stored api key cannot be kept/i),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Keep" })).toBeEnabled();
  });

  it("disables Keep while the destination differs", async () => {
    const user = userEvent.setup();
    renderModal(makeInstance());

    const url = screen.getByLabelText("Server URL");
    await user.clear(url);
    await user.type(url, "https://moved.example");

    expect(screen.getByRole("radio", { name: "Keep" })).toBeDisabled();
  });

  it("keeps a typed key across mode switches", async () => {
    const user = userEvent.setup();
    renderModal(makeInstance());

    await user.click(screen.getByRole("radio", { name: "Replace" }));
    await user.type(screen.getByLabelText("API Key"), "typed-replacement");

    // Switching away and back, including through Keep, never drops it.
    await user.click(screen.getByRole("radio", { name: "Keep" }));
    await user.click(screen.getByRole("radio", { name: "Replace" }));

    expect(screen.getByLabelText("API Key")).toHaveValue("typed-replacement");
  });

  it("shows the server's re-entry message instead of generic retry text", () => {
    mockSaveError = new AxiosError(
      "Request failed",
      "ERR_BAD_REQUEST",
      undefined,
      undefined,
      {
        status: 400,
        data: {
          error_code: "reenter_key",
          message: "Re-enter the key to use a different address",
        },
      } as never,
    );
    renderModal(makeInstance());

    expect(
      screen.getByText("Re-enter the key to use a different address"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/could not save this instance/i),
    ).not.toBeInTheDocument();
  });
});
