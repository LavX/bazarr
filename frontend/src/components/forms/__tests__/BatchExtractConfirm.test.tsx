/* eslint-disable camelcase */

/**
 * Mass extract goes through the same confirm as the other batch actions, and
 * asks for the one backend action that writes every embedded track out.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import BatchModConfirmForm from "@/components/forms/BatchModConfirmForm";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

describe("Batch confirm, Extract Embedded Tracks", () => {
  it("names the action and posts extract for the selection", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post("/api/subtitles/batch", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({
          queued: 2,
          skipped: 0,
          errors: [],
          job_id: 9,
        });
      }),
    );
    const user = userEvent.setup();
    customRender(
      <BatchModConfirmForm
        items={[
          { type: "movie", radarrId: 7, arr_instance_id: 2 },
          { type: "series", sonarrSeriesId: 5, arr_instance_id: 1 },
        ]}
        action="extract"
      />,
    );

    expect(screen.getByText("Extract Embedded Tracks")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Apply to 2 Item(s)" }),
    );

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({
      action: "extract",
      items: [
        { type: "movie", radarrId: 7, arr_instance_id: 2 },
        { type: "series", sonarrSeriesId: 5, arr_instance_id: 1 },
      ],
    });
  });
});
