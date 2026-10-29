import { describe, expect, it } from "vitest";
import { customRender, screen } from "@/tests";
import HistoryIcon from "./HistoryIcon";

describe("HistoryIcon", () => {
  it.each([
    [0, "Delete"],
    [6, "Translated"],
    [7, "Embedded Source"],
    [8, "Extracted"],
    [9, "Manual Score"],
    [99, "Unknown"],
  ])("names action %i %s", (action, label) => {
    customRender(<HistoryIcon action={action} />);

    expect(screen.getByLabelText(label)).toBeInTheDocument();
  });
});
