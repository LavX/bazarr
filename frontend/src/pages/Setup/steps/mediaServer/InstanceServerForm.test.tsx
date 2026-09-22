/* eslint-disable camelcase */

import { FC, useEffect, useRef } from "react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { OnboardingSelectionProvider } from "@/pages/Setup/useOnboardingSelection";
import { useOnboardingSelection } from "@/pages/Setup/useOnboardingSelection";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import InstanceServerForm from "./InstanceServerForm";

const onNext = vi.fn();

// The form is handed its draft, and what this covers is what the draft is
// carrying by the time it is written, so the draft comes from the real
// provider rather than a literal.
const Harness: FC = () => {
  const { drafts, addDraft } = useOnboardingSelection();
  const created = useRef(false);
  useEffect(() => {
    if (created.current) {
      return;
    }
    created.current = true;
    addDraft("jellyfin");
  }, [addDraft]);
  const draft = drafts[0];
  if (!draft) {
    return null;
  }
  return <InstanceServerForm draft={draft} onNext={onNext} onBack={vi.fn()} />;
};

function stageBackend() {
  const asked: string[] = [];
  const created: Record<string, unknown>[] = [];
  server.use(
    http.post("/api/system/media-server-instances/probe", () =>
      HttpResponse.json({ success: true, server_name: "Attic" }),
    ),
    http.post(
      "/api/system/media-server-instances/probe-libraries",
      async ({ request }) => {
        const body = (await request.json()) as { url: string };
        asked.push(body.url);
        return HttpResponse.json({
          data: [{ id: "lib-a", name: "Films", type: "movies", paths: [] }],
          error_code: null,
        });
      },
    ),
    http.post("/api/system/media-server-instances", async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      created.push(body);
      return HttpResponse.json({
        id: "row-1",
        kind: "jellyfin",
        name: String(body.name),
        enabled: true,
        url: String(body.url),
        verify_ssl: true,
        api_key_set: true,
        path_mappings: [],
        refresh_movies: true,
        refresh_episodes: true,
        options: {},
      });
    }),
    http.post(
      "/api/system/settings",
      () => new HttpResponse(null, { status: 204 }),
    ),
  );
  return { asked, created };
}

describe("InstanceServerForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("drops the libraries chosen against the server that answered before", async () => {
    // A library handle belongs to the server that handed it out. Editing a
    // connection field resets the test and libraries hooks, which takes the
    // picker off screen, so the old server's choices sat in the draft with
    // nothing saying so and Continue wrote a library id from one server
    // against another.
    const { asked, created } = stageBackend();
    const user = userEvent.setup();

    customRender(
      <OnboardingSelectionProvider>
        <Harness />
      </OnboardingSelectionProvider>,
    );

    const url = await screen.findByLabelText("Server URL");
    await user.type(url, "http://a.example");
    await user.type(screen.getByLabelText("API Key"), "key-a");

    await user.click(screen.getByRole("button", { name: "Test" }));
    await screen.findByText(/Connected to Attic/);

    await user.click(screen.getByRole("button", { name: "Load libraries" }));
    // Mantine gives the picker a search input and a hidden one, both labelled,
    // and every section keeps its own list mounted, so the option is taken
    // from the list this field controls.
    const fields = await screen.findAllByLabelText("Movie libraries");
    const movies = fields.find(
      (field) => field.getAttribute("type") !== "hidden",
    ) as HTMLInputElement;
    await user.click(movies);
    // A click focuses the field without opening it in jsdom, and the dropdown
    // stays out of the accessibility tree there because its transition never
    // settles. Same treatment as the Discover select helpers.
    if (movies.getAttribute("aria-expanded") !== "true") {
      await user.keyboard("{ArrowDown}");
    }
    // Sections render movies, series and sports in that order, and a sports
    // root can live in a library of any type, so the sports list offers this
    // one too. The first is the movie section's.
    await user.click(
      screen.getAllByRole("option", { name: "Films", hidden: true })[0],
    );
    // Three pickers, and the one that has been answered stops offering the
    // empty placeholder.
    await waitFor(() =>
      expect(screen.getAllByPlaceholderText("None selected")).toHaveLength(2),
    );

    // The reader points the draft at a different server.
    await user.type(screen.getByLabelText("Server URL"), "2");
    expect(screen.queryAllByLabelText("Movie libraries")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Load libraries" }));
    await waitFor(() =>
      expect(screen.getAllByPlaceholderText("None selected")).toHaveLength(3),
    );
    expect(asked).toEqual(["http://a.example", "http://a.example2"]);

    await user.click(screen.getByRole("button", { name: /connect jellyfin/i }));

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0].url).toBe("http://a.example2");
    expect(created[0].options).toEqual({});
  });
});
