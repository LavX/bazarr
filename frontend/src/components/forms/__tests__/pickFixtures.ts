/* eslint-disable camelcase -- API fixtures use the server's field names. */
import { screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { connection, engine, second } from "@/pages/Translation/fixtures";
import server from "@/tests/mocks/node";
import type { TranslationState } from "@/types/translation";

/**
 * What the Translation page would list: an engine with a back end that can take a job, one switched off, one
 * the server marks not usable and one waiting for its key, and a second engine with no back ends. Only the
 * first back end and the second engine can be picked.
 */
export const PICK_STATE: TranslationState = {
  enabled: true,
  engine: 1,
  fallback_engine: null,
  instances: [
    engine({
      connections: [
        connection(1, { model: "usable-model" }),
        connection(2, { model: "off-model", enabled: false }),
        connection(3, { model: "unusable-model", usable: false }),
        connection(4, { model: "keyless-model", awaiting_secrets: true }),
      ],
    }),
    // An engine with no back ends that is ready: picked as a whole.
    second,
  ],
};

/** The status of a server with the new translator on (or off), and the state it answers with. */
export function translatorStatus(
  runtime: boolean,
  availability: { available: boolean; reason: string },
  state: TranslationState = PICK_STATE,
) {
  const asked: string[] = [];
  server.use(
    http.get("/api/system/status", () =>
      HttpResponse.json({
        data: {
          translation_availability: availability,
          editor_translation_availability: availability,
          ...(runtime ? { translation_runtime_v2: true } : {}),
        },
      }),
    ),
    http.get("/api/translation/state", () => {
      asked.push("state");
      return HttpResponse.json(state);
    }),
  );
  return asked;
}

/** Opens a Mantine select by its label and picks the option with this name. */
export async function choose(user: UserEvent, label: string, option: string) {
  const combobox = screen.getByRole("combobox", { name: label });
  await user.click(combobox);
  const dropdown = await waitFor(() => {
    // The dropdown lives outside the rendered container's accessibility tree, so it is reached
    // through the combobox's own controls id.
    // eslint-disable-next-line testing-library/no-node-access
    const el = document.getElementById(
      combobox.getAttribute("aria-controls") ?? "",
    );
    expect(el).not.toBeNull();
    return el!;
  });
  await user.click(
    within(dropdown).getByRole("option", { name: option, hidden: true }),
  );
}

/** The names of the options a Mantine select offers. */
export async function optionsOf(user: UserEvent, label: string) {
  const combobox = screen.getByRole("combobox", { name: label });
  await user.click(combobox);
  const dropdown = await waitFor(() => {
    // eslint-disable-next-line testing-library/no-node-access
    const el = document.getElementById(
      combobox.getAttribute("aria-controls") ?? "",
    );
    expect(el).not.toBeNull();
    return el!;
  });
  return within(dropdown)
    .getAllByRole("option", { hidden: true })
    .map((item) => item.textContent);
}

/** Waits for the Translation page's state, which the pick lists its back ends from. */
export async function stateLoaded() {
  await waitFor(() =>
    expect(
      queryClient.getQueryState([QueryKeys.Translation, "state"])?.status,
    ).toBe("success"),
  );
}
