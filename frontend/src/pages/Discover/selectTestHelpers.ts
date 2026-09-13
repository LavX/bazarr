import { screen, within } from "@testing-library/react";

/** Either a user-event session or the module's direct API: both can act. */
type Actor = {
  click: (element: Element) => Promise<void>;
  keyboard: (text: string) => Promise<unknown>;
};

/**
 * Open a themed select by its label and return its listbox.
 *
 * In jsdom a session click focuses the field without opening it, so the
 * opener falls back to ArrowDown, which is also how a keyboard reader opens
 * it. Mantine's dropdown stays out of the accessibility tree in jsdom, where
 * its transition never settles, so the list and its options are queried with
 * `hidden: true`; the click on an option still goes through Mantine's own
 * submit handler, which is what these tests exercise.
 */
export async function openSelect(actor: Actor, label: string | RegExp) {
  const input = (await screen.findAllByLabelText(label)).find(
    (element): element is HTMLInputElement =>
      element instanceof HTMLInputElement,
  );
  if (!input) throw new Error(`No select input labelled ${String(label)}`);
  const disclosure = input.closest<HTMLDetailsElement>("details");
  if (disclosure && !disclosure.open) {
    const summary = disclosure.querySelector("summary");
    if (summary) await actor.click(summary);
  }
  await actor.click(input);
  if (input.getAttribute("aria-expanded") !== "true")
    await actor.keyboard("{ArrowDown}");
  return screen.findByRole("listbox", { hidden: true });
}

/** Choose an option by its visible text, the way a reader does. */
export async function pickOption(
  actor: Actor,
  label: string | RegExp,
  option: string | RegExp,
) {
  const listbox = await openSelect(actor, label);
  await actor.click(
    within(listbox).getByRole("option", { name: option, hidden: true }),
  );
}

/** A segmented pair is a radio group; choosing is pressing the named radio. */
export async function chooseSegment(actor: Actor, name: string | RegExp) {
  await openSearchOptions(actor);
  await actor.click(screen.getByRole("radio", { name }));
}

/** Reveal the secondary retrieval controls through their visible action. */
export async function openSearchOptions(actor: Actor) {
  const options = screen.queryByRole("button", { name: "Search options" });
  if (options && options.getAttribute("aria-expanded") !== "true")
    await actor.click(options);
}

/** Open the current title's release search, or the global manual fallback. */
export async function openReleaseSearch(actor: Actor) {
  if (screen.queryByRole("button", { name: "Search options" }))
    await openSearchOptions(actor);
  else await actor.click(screen.getByLabelText("Search any movie or show..."));
  await actor.click(
    await screen.findByRole("button", {
      name: "Search providers by release name",
    }),
  );
}

/**
 * The input of a themed select, by label. While its list is open the list is
 * labelled by the same label, so a plain label query would find two elements.
 */
export function selectInput(label: string | RegExp): HTMLInputElement {
  const input = screen
    .getAllByLabelText(label)
    .find(
      (element): element is HTMLInputElement =>
        element instanceof HTMLInputElement,
    );
  if (!input) throw new Error(`No select input labelled ${String(label)}`);
  return input;
}

export async function findSelectInput(label: string | RegExp) {
  await screen.findAllByLabelText(label);
  return selectInput(label);
}
