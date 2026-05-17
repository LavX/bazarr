import { Text } from "@mantine/core";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { useFormActions } from "@/pages/Settings/utilities/FormValues";
import { customRender, screen, waitFor } from "@/tests";
import Layout from "./Layout";

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
});
