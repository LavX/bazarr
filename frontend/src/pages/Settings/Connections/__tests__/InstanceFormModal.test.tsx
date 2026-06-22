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
 * Test: Run with `cd frontend && npx vitest run --reporter=verbose`.
 */

import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type {
  ArrInstanceCreate,
  ArrInstanceUpdate,
} from "@/apis/raw/arrInstances";
import InstanceFormModal from "@/pages/Settings/Connections/InstanceFormModal";
import { customRender, fireEvent, screen, waitFor } from "@/tests";

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
  }> = {},
) {
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
    base_url: "",
    ssl: false,
    verify_ssl: true,
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

function renderAdd(kind: "sonarr" | "radarr" = "sonarr", onClose = vi.fn()) {
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
});
