import { Text } from "@mantine/core";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { useFormActions } from "@/pages/Settings/utilities/FormValues";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import Layout from "./Layout";

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

describe("Settings layout keyboard save", () => {
  it("saves a blur-staged field that is the only change", async () => {
    // The staged count used to be read before the blur, so a field that stages only
    // when it is left was not counted yet when the keystroke arrived. The shortcut
    // then returned without submitting, and silently threw the typing away.
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
        <CommitOnBlurInput />
      </Layout>,
    );

    await user.click(screen.getByLabelText("Commits on blur"));
    await user.keyboard("only-change");
    await user.keyboard("{Control>}s{/Control}");

    await waitFor(() => {
      expect(submitted).toHaveLength(1);
    });
    expect(submitted[0]["settings-general-instance_name"]).toBe("only-change");
  });

  it("submits nothing when there is nothing to save", async () => {
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
        <Text>Nothing staged</Text>
      </Layout>,
    );

    await user.keyboard("{Control>}s{/Control}");

    expect(await screen.findByText("Nothing staged")).toBeInTheDocument();
    expect(submitted).toHaveLength(0);
  });
});
