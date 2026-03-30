import { Text } from "@mantine/core";
import { describe, expect, it } from "vitest";
import { customRender, screen } from "@/tests";
import Layout from "./Layout";

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
});
