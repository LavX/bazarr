/* eslint-disable camelcase */

/**
 * Tests for the InstanceFormModal component.
 *
 * Why: Verifies that the add/edit Sonarr/Radarr instance form renders its
 * fields correctly, calls the create/update mutations with the right payload
 * on submit, and blocks submission when required fields are empty.
 *
 * What: Renders InstanceFormModal with controlled props, uses
 * @testing-library/user-event for interactions, and asserts mutation call
 * arguments via vi.mock on the hooks module. For validation blocking tests,
 * we use fireEvent.submit to check aria-invalid state synchronously before
 * the StrictMode effect cycle clears it.
 *
 * Test: Run with `cd frontend && pnpm exec vitest run --reporter=verbose`.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ArrInstance,
  ArrInstanceCreate,
  ArrInstanceUpdate,
} from "@/apis/raw/arrInstances";
import { forgetPasswordConfirmation } from "@/modules/passwordConfirm";
import InstanceFormModal from "@/pages/Settings/Connections/InstanceFormModal";
import { customRender, fireEvent, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

// ---------------------------------------------------------------------------
// Mock the mutation hooks. We replace the whole @/apis/hooks module so that
// each call to useCreateArrInstance / useUpdateArrInstance returns a spy whose
// .mutate() we can inspect.
// ---------------------------------------------------------------------------

const mockCreateMutate = vi.fn();
const mockUpdateMutate = vi.fn();
const mockTestMutate = vi.fn();
const mockTestByIdMutate = vi.fn();

vi.mock("@/apis/hooks", async (importActual) => {
  const actual = await importActual<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useCreateArrInstance: () => ({
      mutate: mockCreateMutate,
      isPending: false,
      isError: false,
      reset: vi.fn(),
    }),
    useUpdateArrInstance: () => ({
      mutate: mockUpdateMutate,
      isPending: false,
      isError: false,
      reset: vi.fn(),
    }),
    useTestArrInstanceConnection: () => ({
      mutate: mockTestMutate,
      isPending: false,
      isError: false,
      data: undefined,
      reset: vi.fn(),
    }),
    useTestArrInstanceById: () => ({
      mutate: mockTestByIdMutate,
      isPending: false,
      isError: false,
      data: undefined,
      reset: vi.fn(),
    }),
  };
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeInstance(
  overrides: Partial<{
    kind: "sonarr" | "radarr";
    name: string;
    api_key_set: boolean;
    ssl: boolean;
    verify_ssl: boolean;
    base_url: string;
  }> = {},
): ArrInstance {
  return {
    id: 42,
    kind: (overrides.kind ?? "sonarr") as "sonarr" | "radarr",
    stable_key: "stable-abc",
    name: overrides.name ?? "Main Sonarr",
    display_name: overrides.name ?? "Main Sonarr",
    enabled: true,
    is_default: false,
    ip: "192.168.1.10",
    port: 8989,
    base_url: overrides.base_url ?? "",
    ssl: overrides.ssl ?? false,
    verify_ssl: overrides.verify_ssl ?? true,
    http_timeout: 60,
    api_key_set: overrides.api_key_set ?? true,
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Submits the modal's form by dispatching a native submit event on the <form>
 * element that contains the submit button. This is the only reliable way to
 * trigger form-level validation synchronously in jsdom when the form lives
 * inside a Mantine Modal portal (fireEvent.click on the submit button does
 * not bubble correctly in jsdom portals).
 */
function submitForm(submitButtonName: string | RegExp): void {
  const btn = screen.getByRole("button", { name: submitButtonName });
  // eslint-disable-next-line testing-library/no-node-access
  const form = btn.closest("form");
  if (!form) throw new Error("Could not find <form> parent of submit button");
  fireEvent.submit(form);
}

function renderAdd(
  kind: "sonarr" | "radarr" | "sportarr" = "sonarr",
  onClose = vi.fn(),
) {
  customRender(
    <InstanceFormModal opened kind={kind} instance={null} onClose={onClose} />,
  );
}

function renderEdit(
  instance: ReturnType<typeof makeInstance>,
  onClose = vi.fn(),
) {
  customRender(
    <InstanceFormModal
      opened
      kind={instance.kind}
      instance={instance}
      onClose={onClose}
    />,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("InstanceFormModal, add mode", () => {
  it("renders the modal title for Sonarr", () => {
    renderAdd("sonarr");
    expect(screen.getByText("Add Sonarr instance")).toBeInTheDocument();
  });

  it("renders the modal title for Radarr", () => {
    renderAdd("radarr");
    expect(screen.getByText("Add Radarr instance")).toBeInTheDocument();
  });

  it("renders all required form fields", () => {
    renderAdd();
    expect(screen.getByRole("textbox", { name: /name/i })).toBeInTheDocument();
    expect(
      screen.getByRole("textbox", { name: /address/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /port/i })).toBeInTheDocument();
    expect(
      screen.getByRole("textbox", { name: /timeout/i }),
    ).toBeInTheDocument();
  });

  it("renders the kind segmented control so the user can pick Sonarr vs Radarr", () => {
    renderAdd();
    expect(screen.getByRole("radio", { name: /sonarr/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /radarr/i })).toBeInTheDocument();
  });

  it("pre-fills Sonarr default port 8989", () => {
    renderAdd("sonarr");
    expect(screen.getByRole("textbox", { name: /port/i })).toHaveValue("8989");
  });

  it("pre-fills Radarr default port 7878", () => {
    renderAdd("radarr");
    expect(screen.getByRole("textbox", { name: /port/i })).toHaveValue("7878");
  });

  it("calls create mutation with the entered values on submit", async () => {
    const user = userEvent.setup();
    renderAdd("sonarr");

    await user.clear(screen.getByRole("textbox", { name: /name/i }));
    await user.type(
      screen.getByRole("textbox", { name: /name/i }),
      "Test Sonarr",
    );
    await user.clear(screen.getByRole("textbox", { name: /address/i }));
    await user.type(
      screen.getByRole("textbox", { name: /address/i }),
      "10.0.0.1",
    );

    await user.click(screen.getByRole("button", { name: /add instance/i }));

    await waitFor(() => {
      expect(mockCreateMutate).toHaveBeenCalled();
    });

    const [body] = mockCreateMutate.mock.calls[0] as [
      ArrInstanceCreate,
      unknown,
    ];
    expect(body.kind).toBe("sonarr");
    expect(body.name).toBe("Test Sonarr");
    expect(body.ip).toBe("10.0.0.1");
    expect(body.port).toBe(8989);
  });

  it("does NOT call create when Name is empty: marks the field invalid", async () => {
    mockCreateMutate.mockClear();
    renderAdd("sonarr");

    await waitFor(() => {
      expect(
        screen.getByRole("textbox", { name: /name/i }),
      ).toBeInTheDocument();
    });

    // Fill address but leave name blank, then submit.
    // Use fireEvent on the submit button to check the synchronous validation
    // state before the StrictMode portal effect cycle clears errors.
    const addressInput = screen.getByRole("textbox", { name: /address/i });
    fireEvent.change(addressInput, { target: { value: "10.0.0.2" } });

    submitForm(/add instance/i);

    // Validation marks the field invalid synchronously before async effects run.
    const nameInput = screen.getByRole("textbox", { name: /name/i });
    expect(nameInput).toHaveAttribute("aria-invalid", "true");

    // Mutation must never fire when validation fails.
    expect(mockCreateMutate).not.toHaveBeenCalled();
  });

  it("does NOT call create when Address is empty: marks the field invalid", async () => {
    mockCreateMutate.mockClear();
    renderAdd("sonarr");

    await waitFor(() => {
      expect(
        screen.getByRole("textbox", { name: /address/i }),
      ).toBeInTheDocument();
    });

    const nameInput = screen.getByRole("textbox", { name: /name/i });
    fireEvent.change(nameInput, { target: { value: "My Instance" } });

    submitForm(/add instance/i);

    const addressInput = screen.getByRole("textbox", { name: /address/i });
    expect(addressInput).toHaveAttribute("aria-invalid", "true");

    expect(mockCreateMutate).not.toHaveBeenCalled();
  });

  it("includes ssl: true in the payload when the SSL switch is toggled on", async () => {
    const user = userEvent.setup();
    renderAdd("sonarr");

    await waitFor(() => {
      expect(
        screen.getByRole("switch", { name: /use ssl/i }),
      ).toBeInTheDocument();
    });

    await user.type(
      screen.getByRole("textbox", { name: /name/i }),
      "SSL Instance",
    );
    await user.type(
      screen.getByRole("textbox", { name: /address/i }),
      "10.0.0.3",
    );

    // Mantine Switch uses role="switch"
    const sslSwitch = screen.getByRole("switch", { name: /use ssl/i });
    await user.click(sslSwitch);

    await user.click(screen.getByRole("button", { name: /add instance/i }));

    await waitFor(() => {
      expect(mockCreateMutate).toHaveBeenCalled();
    });

    const [body] = mockCreateMutate.mock.calls[0] as [
      ArrInstanceCreate,
      unknown,
    ];
    expect(body.ssl).toBe(true);
  });

  it("switches port default when the user changes kind via the segmented control", async () => {
    const user = userEvent.setup();
    renderAdd("sonarr");

    expect(screen.getByRole("textbox", { name: /port/i })).toHaveValue("8989");

    await user.click(screen.getByRole("radio", { name: /radarr/i }));

    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: /port/i })).toHaveValue(
        "7878",
      );
    });
  });

  it("calls Cancel and does not submit when the Cancel button is clicked", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    mockCreateMutate.mockClear();
    customRender(
      <InstanceFormModal
        opened
        kind="sonarr"
        instance={null}
        onClose={onClose}
      />,
    );

    await user.click(screen.getByRole("button", { name: /cancel/i }));

    expect(onClose).toHaveBeenCalled();
    expect(mockCreateMutate).not.toHaveBeenCalled();
  });
});

describe("InstanceFormModal, edit mode", () => {
  it("renders the modal title with the instance name", () => {
    renderEdit(makeInstance({ name: "Main Sonarr" }));
    expect(screen.getByText("Edit Main Sonarr")).toBeInTheDocument();
  });

  it("pre-fills form fields from the existing instance", async () => {
    renderEdit(makeInstance({ name: "4K Sonarr" }));

    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: /name/i })).toHaveValue(
        "4K Sonarr",
      );
    });
    expect(screen.getByRole("textbox", { name: /address/i })).toHaveValue(
      "192.168.1.10",
    );
    expect(screen.getByRole("textbox", { name: /port/i })).toHaveValue("8989");
  });

  it("does NOT render the kind segmented control (kind is fixed for edit)", () => {
    renderEdit(makeInstance());
    expect(
      screen.queryByRole("radio", { name: /sonarr/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("radio", { name: /radarr/i }),
    ).not.toBeInTheDocument();
  });

  it("renders the Save button (not Add) in edit mode", () => {
    renderEdit(makeInstance());
    expect(
      screen.getByRole("button", { name: /save changes/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /add instance/i }),
    ).not.toBeInTheDocument();
  });

  it("calls update mutation with the correct id and changed name on submit", async () => {
    const user = userEvent.setup();
    mockUpdateMutate.mockClear();
    renderEdit(makeInstance({ name: "Old Name" }));

    const nameInput = screen.getByRole("textbox", { name: /name/i });
    await user.clear(nameInput);
    await user.type(nameInput, "New Name");

    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => {
      expect(mockUpdateMutate).toHaveBeenCalled();
    });

    const [args] = mockUpdateMutate.mock.calls[0] as [
      { id: number; body: ArrInstanceUpdate },
      unknown,
    ];
    expect(args.id).toBe(42);
    expect(args.body.name).toBe("New Name");
    expect(args.body.ip).toBe("192.168.1.10");
    expect(args.body.port).toBe(8989);
  });

  it("does NOT call update when Name is cleared: marks the field invalid", async () => {
    mockUpdateMutate.mockClear();
    renderEdit(makeInstance({ name: "Existing" }));

    await waitFor(() => {
      expect(
        screen.getByRole("textbox", { name: /name/i }),
      ).toBeInTheDocument();
    });

    const nameInput = screen.getByRole("textbox", { name: /name/i });
    fireEvent.change(nameInput, { target: { value: "" } });

    submitForm(/save changes/i);

    expect(nameInput).toHaveAttribute("aria-invalid", "true");
    expect(mockUpdateMutate).not.toHaveBeenCalled();
  });

  it("shows the key-mode segmented control when api_key_set is true", async () => {
    renderEdit(makeInstance({ api_key_set: true }));

    await waitFor(() => {
      // Key-mode controls: three radio options
      expect(
        screen.getByRole("radio", { name: /keep current key/i }),
      ).toBeInTheDocument();
    });
    expect(screen.getByRole("radio", { name: /replace/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /clear/i })).toBeInTheDocument();
  });

  it("shows the Stored badge when api_key_set is true", async () => {
    renderEdit(makeInstance({ api_key_set: true }));

    await waitFor(() => {
      // The Badge with text "Stored" (capital S, standalone badge node)
      const badge = screen.getByText((content, element) => {
        return (
          content === "Stored" && element !== null && element.tagName !== "BODY"
        );
      });
      expect(badge).toBeInTheDocument();
    });
  });

  it("does NOT show the Stored badge when api_key_set is false", () => {
    renderEdit(makeInstance({ api_key_set: false }));
    // The badge text is exactly "Stored" — no key-mode radios either
    expect(
      screen.queryByRole("radio", { name: /keep current key/i }),
    ).not.toBeInTheDocument();
    // Plain API Key label present instead
    expect(screen.getByText("API Key")).toBeInTheDocument();
  });

  it("sends clear_api_key: true in the payload when the user selects Clear", async () => {
    const user = userEvent.setup();
    mockUpdateMutate.mockClear();
    renderEdit(makeInstance({ api_key_set: true }));

    await waitFor(() => {
      expect(screen.getByRole("radio", { name: /clear/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole("radio", { name: /clear/i }));

    await waitFor(() => {
      expect(screen.getByText(/will be removed/i)).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => {
      expect(mockUpdateMutate).toHaveBeenCalled();
    });

    const [args] = mockUpdateMutate.mock.calls[0] as [
      { id: number; body: ArrInstanceUpdate },
      unknown,
    ];
    expect(args.body.clear_api_key).toBe(true);
  });

  it("switches Keep current key off and explains why when the address is typed over", async () => {
    const user = userEvent.setup();
    renderEdit(makeInstance({ api_key_set: true }));

    await waitFor(() => {
      expect(
        screen.getByRole("radio", { name: /keep current key/i }),
      ).toBeInTheDocument();
    });
    // Nothing moved yet: the replace field is not shown.
    expect(
      screen.queryByPlaceholderText("Enter the new API key"),
    ).not.toBeInTheDocument();

    const address = screen.getByRole("textbox", { name: /address/i });
    await user.clear(address);
    await user.type(address, "10.9.9.9");

    await waitFor(() => {
      expect(
        screen.getByPlaceholderText("Enter the new API key"),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/stored key cannot be kept/i)).toBeInTheDocument();
  });

  it("switches Keep current key off when certificate verification is switched off", async () => {
    const user = userEvent.setup();
    // The instance ships with verify_ssl true, so the switch reads as on.
    renderEdit(makeInstance({ api_key_set: true }));

    await waitFor(() => {
      expect(
        screen.getByRole("radio", { name: /keep current key/i }),
      ).toBeInTheDocument();
    });

    await user.click(screen.getByRole("switch", { name: /use ssl/i }));
    await user.click(
      screen.getByRole("switch", { name: /verify certificate/i }),
    );

    await waitFor(() => {
      expect(
        screen.getByPlaceholderText("Enter the new API key"),
      ).toBeInTheDocument();
    });
  });

  it("keeps the stored key when certificate verification is switched back on", async () => {
    const user = userEvent.setup();
    // SSL is already on and verification off, so the only change is turning
    // verification on, which hardens the connection rather than moving it.
    renderEdit(
      makeInstance({ api_key_set: true, ssl: true, verify_ssl: false }),
    );

    await user.click(
      screen.getByRole("switch", { name: /verify certificate/i }),
    );

    expect(
      screen.queryByPlaceholderText("Enter the new API key"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/stored key cannot be kept/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("radio", { name: /keep current key/i }),
    ).toBeEnabled();
  });

  it("keeps the stored key for a stored trailing-slash base URL and submits the normalized path", async () => {
    const user = userEvent.setup();
    mockUpdateMutate.mockClear();
    // The stored spelling carries the trailing slash the client drops when it
    // builds the request URL, and the form normalizes the same way: the two
    // spellings are one path, so the stored key is kept and the save carries
    // the normalized one.
    renderEdit(makeInstance({ api_key_set: true, base_url: "/sonarr/" }));

    await waitFor(() => {
      expect(
        screen.getByRole("radio", { name: /keep current key/i }),
      ).toBeInTheDocument();
    });
    expect(
      screen.queryByPlaceholderText("Enter the new API key"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/stored key cannot be kept/i),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => {
      expect(mockUpdateMutate).toHaveBeenCalled();
    });
    const [args] = mockUpdateMutate.mock.calls[0] as [
      { id: number; body: ArrInstanceUpdate },
      unknown,
    ];
    expect(args.body.base_url).toBe("/sonarr");
    expect(args.body).not.toHaveProperty("clear_api_key");
    expect(args.body).not.toHaveProperty("api_key");
  });

  it("does not keep the stored key for a repeated trailing slash the save would refuse", async () => {
    // The stored spelling ends in two slashes, and the client drops only one
    // when it builds its request URL, so the stored path stays '/sonarr/',
    // while the form's own spelling submits '/sonarr'. The comparison used to
    // strip every boundary slash on both sides, so the stored key was kept
    // for a destination the save refuses; the outgoing form path is now read
    // against the stored path the way the client builds it, so the key is
    // asked for from the moment the row loads.
    renderEdit(makeInstance({ api_key_set: true, base_url: "/sonarr//" }));

    await waitFor(() => {
      expect(
        screen.getByPlaceholderText("Enter the new API key"),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/stored key cannot be kept/i)).toBeInTheDocument();
    expect(
      screen.getByRole("radio", { name: /keep current key/i }),
    ).toBeDisabled();
  });

  it("switches Keep current key off when verification is turned off with SSL already on", async () => {
    const user = userEvent.setup();
    renderEdit(
      makeInstance({ api_key_set: true, ssl: true, verify_ssl: true }),
    );

    await waitFor(() => {
      expect(
        screen.getByRole("radio", { name: /keep current key/i }),
      ).toBeInTheDocument();
    });

    await user.click(
      screen.getByRole("switch", { name: /verify certificate/i }),
    );

    await waitFor(() => {
      expect(
        screen.getByPlaceholderText("Enter the new API key"),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/stored key cannot be kept/i)).toBeInTheDocument();
    expect(
      screen.getByRole("radio", { name: /keep current key/i }),
    ).toBeDisabled();
  });

  it("keeps a typed replacement key across mode switches", async () => {
    const user = userEvent.setup();
    renderEdit(makeInstance({ api_key_set: true }));

    await user.click(screen.getByRole("radio", { name: /replace/i }));
    const key = screen.getByPlaceholderText("Enter the new API key");
    await user.type(key, "typed-replacement");

    // Switching away and back, including through Keep, never drops it.
    await user.click(screen.getByRole("radio", { name: /keep current key/i }));
    await user.click(screen.getByRole("radio", { name: /replace/i }));

    expect(screen.getByPlaceholderText("Enter the new API key")).toHaveValue(
      "typed-replacement",
    );
  });
});

// ---------------------------------------------------------------------------
// Per-instance default language profile
// ---------------------------------------------------------------------------

describe("InstanceFormModal, default language profile", () => {
  it.each(["sonarr", "radarr", "sportarr"] as const)(
    "starts on the global-default choice for %s",
    (kind) => {
      renderAdd(kind);
      expect(
        screen.getByRole("combobox", { name: /profile for newly synced/i }),
      ).toHaveValue("Use the global default");
    },
  );

  it("omits media_defaults from the create payload when nothing is picked", async () => {
    const user = userEvent.setup();
    mockCreateMutate.mockClear();
    renderAdd("sonarr");

    await user.type(screen.getByRole("textbox", { name: /name/i }), "Plain");
    await user.type(
      screen.getByRole("textbox", { name: /address/i }),
      "10.0.0.9",
    );
    await user.click(screen.getByRole("button", { name: /add instance/i }));

    await waitFor(() => {
      expect(mockCreateMutate).toHaveBeenCalled();
    });
    const [body] = mockCreateMutate.mock.calls[0] as [
      ArrInstanceCreate,
      unknown,
    ];
    // No override means no block at all, so the instance inherits the global
    // default rather than pinning whatever the form happened to show.
    expect(body.media_defaults).toBeUndefined();
  });

  it("sends a cleared media_defaults block when editing an instance with no override", async () => {
    const user = userEvent.setup();
    mockUpdateMutate.mockClear();
    renderEdit(makeInstance());

    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => {
      expect(mockUpdateMutate).toHaveBeenCalled();
    });
    const [args] = mockUpdateMutate.mock.calls[0] as [
      { id: number; body: ArrInstanceUpdate },
      unknown,
    ];
    expect(args.body.media_defaults).toEqual({});
  });

  it("keeps the saved override selected when editing", () => {
    renderEdit({
      ...makeInstance(),
      media_defaults: { default_enabled: false },
    });
    expect(
      screen.getByRole("combobox", { name: /profile for newly synced/i }),
    ).toHaveValue("No profile");
  });

  it("spells out the precedence in the field description", () => {
    renderAdd("sonarr");
    expect(
      screen.getByText(
        /Sonarr tag still wins over this, and this wins over the global/i,
      ),
    ).toBeInTheDocument();
  });

  it("offers the apply action only on a saved instance that assigns a profile", () => {
    renderEdit(makeInstance());
    expect(
      screen.getByRole("button", { name: /apply to unset/i }),
    ).toBeDisabled();

    renderEdit({
      ...makeInstance(),
      media_defaults: { default_enabled: true, default_profile: 3 },
    });
    expect(
      screen.getAllByRole("button", { name: /apply to unset/i }).at(-1),
    ).toBeEnabled();
  });

  it("hides the apply action entirely while adding an instance", () => {
    renderAdd("sonarr");
    expect(
      screen.queryByRole("button", { name: /apply to unset/i }),
    ).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// The post-processing override save asks for the password confirmation
// ---------------------------------------------------------------------------

describe("InstanceFormModal, post-processing confirmation", () => {
  const FORM_AUTH_SETTINGS = http.get("/api/system/settings", () =>
    HttpResponse.json({
      general: { theme: "auto" },
      auth: { type: "form", username: "admin", password: "***" },
    }),
  );

  function mockConfirmEndpoint() {
    return http.post("/api/system/account/confirm", () => {
      return new HttpResponse(null, { status: 204 });
    });
  }

  beforeEach(() => {
    forgetPasswordConfirmation();
    server.use(FORM_AUTH_SETTINGS, mockConfirmEndpoint());
  });

  function makeInstanceWithOverrides(
    subtitle_settings: LooseObject,
  ): ArrInstance {
    return { ...makeInstance(), subtitle_settings };
  }

  async function enablePostprocessingOverride(
    user: ReturnType<typeof userEvent.setup>,
  ) {
    await user.click(
      screen.getByRole("switch", { name: /use custom post-processing/i }),
    );
    await user.click(screen.getByRole("radio", { name: "Enabled" }));
  }

  it("asks for the password when the save turns the override on", async () => {
    const user = userEvent.setup();
    mockUpdateMutate.mockClear();
    renderEdit(makeInstanceWithOverrides({}));

    await enablePostprocessingOverride(user);
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    expect(mockUpdateMutate).not.toHaveBeenCalled();
    expect(
      await screen.findByText("Confirm your password"),
    ).toBeInTheDocument();
  });

  it("saves after the password is confirmed", async () => {
    const user = userEvent.setup();
    mockUpdateMutate.mockClear();
    renderEdit(makeInstanceWithOverrides({}));

    await enablePostprocessingOverride(user);
    await user.click(screen.getByRole("button", { name: /save changes/i }));
    await user.type(await screen.findByLabelText("Password"), "hunter2");
    await user.click(screen.getByRole("button", { name: /^confirm$/i }));

    await waitFor(() => {
      expect(mockUpdateMutate).toHaveBeenCalled();
    });
    const { body } = mockUpdateMutate.mock.calls[0][0];
    expect(body.subtitle_settings.general.use_postprocessing).toBe(true);
  });

  it("does not save when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    mockUpdateMutate.mockClear();
    renderEdit(makeInstanceWithOverrides({}));

    await enablePostprocessingOverride(user);
    await user.click(screen.getByRole("button", { name: /save changes/i }));
    const prompt = await screen.findByRole("dialog", {
      name: "Confirm your password",
    });
    await user.click(within(prompt).getByRole("button", { name: /cancel/i }));

    await waitFor(() => {
      expect(
        screen.queryByText("Confirm your password"),
      ).not.toBeInTheDocument();
    });
    expect(mockUpdateMutate).not.toHaveBeenCalled();
  });

  it("does not ask for the password when the override is unchanged", async () => {
    const user = userEvent.setup();
    mockUpdateMutate.mockClear();
    // The stored override says post-processing is on with this command, and
    // the modal resends it untouched: no prompt, the save just goes through.
    renderEdit(
      makeInstanceWithOverrides({
        general: { use_postprocessing: true, postprocessing_cmd: "/bin/true" },
      }),
    );

    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => {
      expect(mockUpdateMutate).toHaveBeenCalled();
    });
    expect(screen.queryByText("Confirm your password")).not.toBeInTheDocument();
  });
});
