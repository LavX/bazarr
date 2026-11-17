import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { SortableList } from "@/components/plugin-settings";
import { customRender, screen } from "@/tests";

/**
 * A list the test can reorder from outside. With `applies` off it takes a move and does nothing with
 * it, as a caller does that waits for a save, or refuses the move.
 */
function Rows({ applies }: { applies: boolean }) {
  const [order, setOrder] = useState(["a", "b", "c"]);
  return (
    <>
      <SortableList
        items={order}
        getId={(item) => item}
        getName={(item) => item.toUpperCase()}
        renderItem={(item) => <span>{item}</span>}
        onReorder={(ids) => {
          if (applies) {
            setOrder(ids);
          }
        }}
      />
      <button type="button" onClick={() => setOrder(["b", "c", "a"])}>
        Send A to the end
      </button>
    </>
  );
}

const names = () =>
  screen
    .getAllByRole("button", { name: /^Move . up$/ })
    .map((item) => item.getAttribute("aria-label"));

describe("SortableList", () => {
  it("keeps the focus on the row that was moved to an end, where the button pressed goes off", async () => {
    const user = userEvent.setup();
    customRender(<Rows applies />);
    await user.click(screen.getByRole("button", { name: "Move B up" }));
    expect(names()).toEqual(["Move B up", "Move A up", "Move C up"]);
    // "Move B up" is off at the top: the focus is on the row's other button, not lost.
    expect(screen.getByRole("button", { name: "Move B up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move B down" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Move A down" }));
    expect(names()).toEqual(["Move B up", "Move C up", "Move A up"]);
    expect(screen.getByRole("button", { name: "Move A up" })).toHaveFocus();
  });

  it("forgets a press whose move was not applied, so a later change of order does not move the focus", async () => {
    const user = userEvent.setup();
    customRender(<Rows applies={false} />);
    // Pressed, and nothing was drawn for it: the order is as it was.
    await user.click(screen.getByRole("button", { name: "Move B up" }));
    expect(names()).toEqual(["Move A up", "Move B up", "Move C up"]);
    // Something else changes the order and B ends up first. That is not B's move.
    const other = screen.getByRole("button", { name: "Send A to the end" });
    await user.click(other);
    expect(names()).toEqual(["Move B up", "Move C up", "Move A up"]);
    expect(other).toHaveFocus();
    expect(
      screen.getByRole("button", { name: "Move B down" }),
    ).not.toHaveFocus();
  });
});
