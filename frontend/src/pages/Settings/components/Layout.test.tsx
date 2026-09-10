import { createMemoryRouter, Link, RouterProvider } from "react-router";
import { Text } from "@mantine/core";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { useFormActions } from "@/pages/Settings/utilities/FormValues";
import { AllProviders } from "@/providers";
import { customRender, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { Password } from "./forms";
import Layout from "./Layout";
import LayoutModal from "./LayoutModal";

// An input that only stages its value when it is left, the way the AI Model
// field commits an OpenRouter routing shortcut.
function CommitOnBlurInput() {
  const { setValue } = useFormActions();

  return (
    <input
      aria-label="Commits on blur"
      onBlur={(e) =>
        setValue(e.currentTarget.value, "settings-general-instance_name")
      }
    />
  );
}

function StageChangeButton() {
  const { setValue } = useFormActions();

  return (
    <button
      type="button"
      onClick={() => setValue("changed", "settings-general-instance_name")}
    >
      Stage change
    </button>
  );
}

describe("Settings layout", () => {
  it.concurrent("should be able to render without issues", () => {
    customRender(
      <Layout name="Test Settings">
        <Text>Value</Text>
      </Layout>,
    );
  });

  it.concurrent(
    "save button should not be visible when no changes are staged",
    () => {
      customRender(
        <Layout name="Test Settings">
          <Text>Value</Text>
        </Layout>,
      );

      // The floating save button is hidden when totalStagedCount === 0
      expect(
        screen.queryByRole("button", { name: /save/i }),
      ).not.toBeInTheDocument();
    },
  );

  it.concurrent("renders children content", () => {
    customRender(
      <Layout name="Test Settings">
        <Text>Test Content</Text>
      </Layout>,
    );

    expect(screen.getByText("Test Content")).toBeInTheDocument();
  });

  it("can render a fluid content area", () => {
    customRender(
      <Layout name="Test Settings" fluid>
        <Text>Test Content</Text>
      </Layout>,
    );

    expect(screen.getByTestId("settings-layout-content")).toHaveStyle({
      maxWidth: "none",
      width: "100%",
    });
  });

  it("shows a readable pending-change count on the floating save button", async () => {
    customRender(
      <Layout name="Test Settings">
        <StageChangeButton />
      </Layout>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Stage change" }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Save 1 pending change" }),
      ).toBeInTheDocument();
    });
    expect(screen.getByLabelText("1 unsaved change")).toHaveTextContent("1");
  });

  it("commits the focused field before a keyboard save submits", async () => {
    const submitted: LooseObject[] = [];
    server.use(
      http.post("/api/system/settings", async ({ request }) => {
        const form = await request.formData();
        submitted.push(Object.fromEntries(form.entries()));
        return HttpResponse.json({});
      }),
    );

    const user = userEvent.setup();
    customRender(
      <Layout name="Test Settings">
        <StageChangeButton />
        <CommitOnBlurInput />
      </Layout>,
    );

    // Something has to be staged, or the shortcut does nothing at all.
    await user.click(screen.getByRole("button", { name: "Stage change" }));

    const input = screen.getByLabelText("Commits on blur");
    await user.click(input);
    await user.keyboard("typed-while-focused");
    await user.keyboard("{Control>}s{/Control}");

    await waitFor(() => {
      expect(submitted).toHaveLength(1);
    });
    expect(submitted[0]["settings-general-instance_name"]).toBe(
      "typed-while-focused",
    );
  });

  it("commits the focused field when Enter submits the form", async () => {
    const submitted: LooseObject[] = [];
    server.use(
      http.post("/api/system/settings", async ({ request }) => {
        const form = await request.formData();
        submitted.push(Object.fromEntries(form.entries()));
        return HttpResponse.json({});
      }),
    );

    const user = userEvent.setup();
    customRender(
      <Layout name="Test Settings">
        <StageChangeButton />
        <CommitOnBlurInput />
      </Layout>,
    );

    await user.click(screen.getByRole("button", { name: "Stage change" }));

    const input = screen.getByLabelText("Commits on blur");
    await user.click(input);
    await user.keyboard("typed-then-enter{Enter}");

    await waitFor(() => {
      expect(submitted).toHaveLength(1);
    });
    expect(submitted[0]["settings-general-instance_name"]).toBe(
      "typed-then-enter",
    );
  });
});

it("submits a secret while the active development logger records only field names", async () => {
  vi.stubEnv("MODE", "development");
  const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
  const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
  const submitted: LooseObject[] = [];
  server.use(
    http.post("/api/system/settings", async ({ request }) => {
      submitted.push(Object.fromEntries((await request.formData()).entries()));
      return HttpResponse.json({});
    }),
  );
  try {
    customRender(
      <Layout name="Logging test">
        <CommitOnBlurInput />
      </Layout>,
    );
    await userEvent.type(
      screen.getByLabelText("Commits on blur"),
      "sentinel-layout-secret{Enter}",
    );
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]["settings-general-instance_name"]).toBe(
      "sentinel-layout-secret",
    );
    expect(log).toHaveBeenCalledWith("[info] submitting settings", [
      "settings-general-instance_name",
    ]);
    expect(
      JSON.stringify([log.mock.calls, warn.mock.calls, error.mock.calls]),
    ).not.toContain("sentinel-layout-secret");
  } finally {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  }
});

it("keeps modal submission values out of the active development logger", async () => {
  vi.stubEnv("MODE", "development");
  const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
  const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
  const submitted: LooseObject[] = [];
  const close = vi.fn();
  server.use(
    http.post("/api/system/settings", async ({ request }) => {
      submitted.push(Object.fromEntries((await request.formData()).entries()));
      return HttpResponse.json({});
    }),
  );
  try {
    customRender(
      <LayoutModal callbackModal={close}>
        <Password label="API Key" settingKey="settings-silo-apikey" />
      </LayoutModal>,
    );
    await userEvent.type(
      screen.getByLabelText("API Key"),
      "sentinel-modal-secret",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]["settings-silo-apikey"]).toBe("sentinel-modal-secret");
    expect(log).toHaveBeenCalledWith("[info] submitting settings", [
      "settings-silo-apikey",
    ]);
    expect(
      JSON.stringify([log.mock.calls, warn.mock.calls, error.mock.calls]),
    ).not.toContain("sentinel-modal-secret");
    await waitFor(() => expect(close).toHaveBeenCalledWith(true));
  } finally {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  }
});

it("keeps Save and Leave values out of the active development logger", async () => {
  vi.stubEnv("MODE", "development");
  const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
  const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
  const submitted: LooseObject[] = [];
  server.use(
    http.post("/api/system/settings", async ({ request }) => {
      submitted.push(Object.fromEntries((await request.formData()).entries()));
      return HttpResponse.json({});
    }),
  );
  const router = createMemoryRouter([
    {
      path: "/",
      element: (
        <Layout name="Logging test">
          <Password label="API Key" settingKey="settings-emby-apikey" />
          <Link to="/away">Leave settings</Link>
        </Layout>
      ),
    },
    { path: "/away", element: <p>Destination</p> },
  ]);
  try {
    rawRender(
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>,
    );
    await userEvent.type(
      screen.getByLabelText("API Key"),
      "sentinel-leave-secret",
    );
    await userEvent.click(screen.getByRole("link", { name: "Leave settings" }));
    await userEvent.click(
      await screen.findByRole("button", {
        name: "Save and leave",
      }),
    );
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(await screen.findByText("Destination")).toBeInTheDocument();
    expect(submitted[0]["settings-emby-apikey"]).toBe("sentinel-leave-secret");
    expect(log).toHaveBeenCalledWith("[info] save & leave", [
      "settings-emby-apikey",
    ]);
    expect(
      JSON.stringify([log.mock.calls, warn.mock.calls, error.mock.calls]),
    ).not.toContain("sentinel-leave-secret");
  } finally {
    router.dispose();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  }
});
