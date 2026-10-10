import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/** Open a Mantine select in jsdom and return the listbox controlled by its input. */
export async function openSelect(
  user: ReturnType<typeof userEvent.setup>,
  placeholder: string,
) {
  const input = await screen.findByPlaceholderText(placeholder);
  await user.click(input);
  if (input.getAttribute("aria-expanded") !== "true")
    await user.keyboard("{ArrowDown}");

  const controlled = input.getAttribute("aria-controls");
  const listboxes = await screen.findAllByRole("listbox", { hidden: true });
  const listbox =
    listboxes.find((box) => box.getAttribute("id") === controlled) ??
    listboxes[0];
  if (!listbox)
    throw new Error(`Could not find the listbox for "${placeholder}"`);

  return listbox;
}
