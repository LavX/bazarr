/* eslint-disable camelcase */
/**
 * Tests for the InstanceCard component.
 *
 * Why: Verifies that the card renders instance metadata (name, base URL, API
 * key status, timeout, badges) and that clicking action controls (Edit, Test,
 * Delete via overflow menu, enable/disable toggle) invokes the right handlers
 * with the correct arguments.
 *
 * What: Renders InstanceCard directly with a controlled ArrInstance fixture and
 * spy handlers. MSW handles the PATCH /api/system/arr-instances/:id (toggle)
 * and POST /api/system/arr-instances/:id/test (Test button) endpoints.
 *
 * Test: Run with `cd frontend && npx vitest run --reporter=verbose`.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import type { ArrInstance } from "@/apis/raw/arrInstances";
import InstanceCard from "@/pages/Settings/Connections/InstanceCard";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

// ---------------------------------------------------------------------------
// Shared fixture
// ---------------------------------------------------------------------------

function makeInstance(overrides: Partial<ArrInstance> = {}): ArrInstance {
  return {
    id: 42,
    kind: "sonarr",
    stable_key: "test-stable-key",
    name: "Main Sonarr",
    display_name: "Main Sonarr",
    enabled: true,
    is_default: false,
    ip: "192.168.1.10",
    port: 8989,
    base_url: "",
    ssl: false,
    verify_ssl: false,
    http_timeout: 30,
    api_key_set: true,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function setupTestEndpoint(result: {
  ok: boolean;
  version?: string;
  app_name?: string;
  error?: string;
  message?: string;
}) {
  server.use(
    http.post("/api/system/arr-instances/42/test", () =>
      HttpResponse.json(result),
    ),
  );
}

function setupUpdateEndpoint() {
  server.use(
    http.patch("/api/system/arr-instances/42", () =>
      HttpResponse.json({ ...makeInstance(), enabled: false }),
    ),
  );
}

// ---------------------------------------------------------------------------
// Rendering: basic metadata
// ---------------------------------------------------------------------------

describe("InstanceCard: renders instance metadata", () => {
  it("shows the instance name", () => {
    const instance = makeInstance();
    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(screen.getByText("Main Sonarr")).toBeInTheDocument();
  });

  it("shows the constructed host URL", () => {
    const instance = makeInstance();
    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    // buildHostUrl: http://192.168.1.10:8989
    expect(screen.getByText("http://192.168.1.10:8989")).toBeInTheDocument();
  });

  it("shows 'Key stored' when api_key_set is true", () => {
    const instance = makeInstance({ api_key_set: true });
    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(screen.getByText(/Key stored/)).toBeInTheDocument();
  });

  it("shows 'No API key' when api_key_set is false", () => {
    const instance = makeInstance({ api_key_set: false });
    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(screen.getByText(/No API key/)).toBeInTheDocument();
  });

  it("shows the configured timeout", () => {
    const instance = makeInstance({ http_timeout: 60 });
    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(screen.getByText(/Timeout 60s/)).toBeInTheDocument();
  });

  it("shows a Default badge for the default instance", () => {
    const instance = makeInstance({ is_default: true });
    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(screen.getByText("Default")).toBeInTheDocument();
  });

  it("shows a Disabled badge when the instance is disabled", () => {
    const instance = makeInstance({ enabled: false });
    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(screen.getByText("Disabled")).toBeInTheDocument();
  });

  it("shows display_name in parens when it differs from name", () => {
    const instance = makeInstance({
      name: "Sonarr 4K",
      display_name: "The 4K Instance",
    });
    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(screen.getByText("Sonarr 4K")).toBeInTheDocument();
    expect(screen.getByText("(The 4K Instance)")).toBeInTheDocument();
  });

  it("shows SSL info when ssl is true and verify_ssl is false", () => {
    const instance = makeInstance({ ssl: true, verify_ssl: false });
    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(screen.getByText(/SSL, unverified/)).toBeInTheDocument();
  });

  it("shows SSL verified info when ssl and verify_ssl are both true", () => {
    const instance = makeInstance({ ssl: true, verify_ssl: true });
    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(screen.getByText(/SSL, verified/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Action: Edit button calls onEdit with the instance
// ---------------------------------------------------------------------------

describe("InstanceCard: Edit button", () => {
  it("calls onEdit with the instance when clicked", async () => {
    const user = userEvent.setup();
    const instance = makeInstance();
    const onEdit = vi.fn();

    customRender(
      <InstanceCard instance={instance} onEdit={onEdit} onDelete={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: /^Edit$/i }));

    expect(onEdit).toHaveBeenCalledOnce();
    expect(onEdit).toHaveBeenCalledWith(instance);
  });
});

// ---------------------------------------------------------------------------
// Action: Delete via overflow menu calls onDelete with the instance
// ---------------------------------------------------------------------------

describe("InstanceCard: Delete via overflow menu", () => {
  it("calls onDelete with the instance when Delete menu item is clicked", async () => {
    const user = userEvent.setup();
    const instance = makeInstance();
    const onDelete = vi.fn();

    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={onDelete} />,
    );

    // Open the "..." menu
    await user.click(
      screen.getByRole("button", {
        name: /More actions for Main Sonarr/i,
      }),
    );

    // Click the Delete item
    await user.click(await screen.findByRole("menuitem", { name: /Delete/i }));

    expect(onDelete).toHaveBeenCalledOnce();
    expect(onDelete).toHaveBeenCalledWith(instance);
  });
});

// ---------------------------------------------------------------------------
// Action: Test button fires the test-existing API and shows success status
// ---------------------------------------------------------------------------

describe("InstanceCard: Test button shows connection result", () => {
  it("shows a Connected status after a successful test response", async () => {
    setupTestEndpoint({ ok: true, version: "4.0.9", app_name: "Sonarr" });

    const user = userEvent.setup();
    const instance = makeInstance();

    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: /^Test$/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/Connected: Sonarr v4\.0\.9/),
      ).toBeInTheDocument();
    });
  });

  it("shows a failure status when the test response is not ok", async () => {
    setupTestEndpoint({ ok: false, message: "Connection refused" });

    const user = userEvent.setup();
    const instance = makeInstance();

    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: /^Test$/i }));

    await waitFor(() => {
      expect(screen.getByText(/Connection refused/)).toBeInTheDocument();
    });
  });

  it("shows a 'No API key stored' warning when error is unauthorized and no key is set", async () => {
    setupTestEndpoint({ ok: false, error: "unauthorized" });

    const user = userEvent.setup();
    const instance = makeInstance({ api_key_set: false });

    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: /^Test$/i }));

    await waitFor(() => {
      expect(screen.getByText(/No API key stored/)).toBeInTheDocument();
    });
  });

  it("dismisses the test result when the dismiss button is clicked", async () => {
    setupTestEndpoint({ ok: true, version: "4.0.9", app_name: "Sonarr" });

    const user = userEvent.setup();
    const instance = makeInstance();

    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: /^Test$/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/Connected: Sonarr v4\.0\.9/),
      ).toBeInTheDocument();
    });

    await user.click(
      screen.getByRole("button", { name: /Dismiss test result/i }),
    );

    await waitFor(() => {
      expect(
        screen.queryByText(/Connected: Sonarr v4\.0\.9/),
      ).not.toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Action: Enable/Disable toggle fires the update mutation
// ---------------------------------------------------------------------------

describe("InstanceCard: enable/disable toggle", () => {
  it("fires the update mutation with enabled:false when toggling off", async () => {
    setupUpdateEndpoint();

    // Capture the PATCH request body so we can assert what was sent.
    let patchBody: unknown = null;
    server.use(
      http.patch("/api/system/arr-instances/42", async ({ request }) => {
        patchBody = await request.json();
        return HttpResponse.json({ ...makeInstance(), enabled: false });
      }),
    );

    const user = userEvent.setup();
    const instance = makeInstance({ enabled: true });

    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );

    // Mantine Switch renders as a button (role="switch") with the aria-label
    const toggle = screen.getByRole("switch", { name: /Disable instance/i });
    await user.click(toggle);

    await waitFor(() => {
      expect(patchBody).toMatchObject({ enabled: false });
    });
  });
});

// ---------------------------------------------------------------------------
// Action: Set as default via overflow menu fires the update mutation
// ---------------------------------------------------------------------------

describe("InstanceCard: Set as default menu item", () => {
  it("fires the update mutation with is_default:true when Set as default is clicked", async () => {
    let patchBody: unknown = null;
    server.use(
      http.patch("/api/system/arr-instances/42", async ({ request }) => {
        patchBody = await request.json();
        return HttpResponse.json({ ...makeInstance(), is_default: true });
      }),
    );

    const user = userEvent.setup();
    const instance = makeInstance({ is_default: false });

    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );

    await user.click(
      screen.getByRole("button", { name: /More actions for Main Sonarr/i }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: /Set as default/i }),
    );

    await waitFor(() => {
      expect(patchBody).toMatchObject({ is_default: true });
    });
  });

  it("disables the Set as default item when already the default", async () => {
    const user = userEvent.setup();
    const instance = makeInstance({ is_default: true });

    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );

    await user.click(
      screen.getByRole("button", { name: /More actions for Main Sonarr/i }),
    );

    const setDefault = await screen.findByRole("menuitem", {
      name: /Set as default/i,
    });
    expect(setDefault).toHaveAttribute("data-disabled", "true");
  });
});

// ---------------------------------------------------------------------------
// Webhook URL row is always rendered
// ---------------------------------------------------------------------------

describe("InstanceCard: webhook URL", () => {
  it("renders the webhook URL row for the instance", () => {
    const instance = makeInstance({ stable_key: "abc123" });
    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    // The webhook URL includes the kind and stable_key
    const webhookEl = screen.getByText(/webhooks\/sonarr\/abc123/);
    expect(webhookEl).toBeInTheDocument();
  });

  it("renders a copy webhook URL button", () => {
    const instance = makeInstance();
    customRender(
      <InstanceCard instance={instance} onEdit={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(
      screen.getByRole("button", { name: /Copy webhook URL/i }),
    ).toBeInTheDocument();
  });
});
