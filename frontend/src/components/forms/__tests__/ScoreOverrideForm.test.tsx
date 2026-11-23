/* eslint-disable camelcase -- API fixtures use the server's field names. */
/**
 * The score form types a whole percentage and posts it for the listed file.
 * Typing, not pasting: the input commits on change.
 */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import ScoreOverrideForm from "@/components/forms/ScoreOverrideForm";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const selections: FormType.ModifySubtitle[] = [
  {
    type: "movie",
    id: 42,
    path: "/movies/a.en.srt",
    language: "en",
    arr_instance_id: 3,
  },
];

describe("ScoreOverrideForm", () => {
  it("posts the typed score for the listed file", async () => {
    const user = userEvent.setup();
    const bodies: FormData[] = [];
    server.use(
      http.post("/api/subtitles/score-override", async ({ request }) => {
        bodies.push(await request.formData());
        return new HttpResponse(null, { status: 204 });
      }),
    );
    customRender(<ScoreOverrideForm selections={selections} />);

    const input = screen.getByRole("textbox", { name: "Score (%)" });
    await user.clear(input);
    await user.type(input, "64");
    await user.click(screen.getByRole("button", { name: "Set score" }));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0].get("score")).toBe("64");
    expect(bodies[0].get("radarrMovieId")).toBe("42");
    expect(bodies[0].get("subtitlesPath")).toBe("/movies/a.en.srt");
  });

  it.each(["movie", "sports"] as const)(
    "clamps a %s score above 100 to 100 before it posts",
    async (type) => {
      const user = userEvent.setup();
      const bodies: FormData[] = [];
      server.use(
        http.post("/api/subtitles/score-override", async ({ request }) => {
          bodies.push(await request.formData());
          return new HttpResponse(null, { status: 204 });
        }),
      );
      customRender(
        <ScoreOverrideForm selections={[{ ...selections[0], type }]} />,
      );

      const input = screen.getByRole("textbox", { name: "Score (%)" });
      await user.clear(input);
      await user.type(input, "101");
      await user.click(screen.getByRole("button", { name: "Set score" }));

      await waitFor(() => expect(bodies).toHaveLength(1));
      expect(bodies[0].get("score")).toBe("100");
    },
  );

  it("refuses an empty score without posting", async () => {
    const user = userEvent.setup();
    let posted = 0;
    server.use(
      http.post("/api/subtitles/score-override", () => {
        posted += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    customRender(<ScoreOverrideForm selections={selections} />);

    await user.clear(screen.getByRole("textbox", { name: "Score (%)" }));
    await user.click(screen.getByRole("button", { name: "Set score" }));

    expect(
      await screen.findByText("The score must be a whole number from 0 to 100"),
    ).toBeInTheDocument();
    expect(posted).toBe(0);
  });

  it.each([0, 100])(
    "posts a sports score of %s for its owned file",
    async (score) => {
      const user = userEvent.setup();
      const bodies: FormData[] = [];
      server.use(
        http.post("/api/subtitles/score-override", async ({ request }) => {
          bodies.push(await request.formData());
          return new HttpResponse(null, { status: 204 });
        }),
      );
      customRender(
        <ScoreOverrideForm
          selections={[{ ...selections[0], type: "sports" }]}
        />,
      );

      const input = screen.getByRole("textbox", { name: "Score (%)" });
      await user.clear(input);
      await user.type(input, String(score));
      await user.click(screen.getByRole("button", { name: "Set score" }));

      await waitFor(() => expect(bodies).toHaveLength(1));
      expect(bodies[0].get("sportsEventId")).toBe("42");
      expect(bodies[0].get("arr_instance_id")).toBe("3");
      expect(bodies[0].get("subtitlesPath")).toBe("/movies/a.en.srt");
      expect(bodies[0].get("score")).toBe(String(score));
      expect(bodies[0].has("radarrMovieId")).toBe(false);
      expect(bodies[0].has("sonarrEpisodeId")).toBe(false);
    },
  );
});
