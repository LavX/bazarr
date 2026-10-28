/* eslint-disable camelcase */

/**
 * Mass OCR goes through the same confirm as the other batch actions. It says
 * which tracks it reads, because the action's name alone does not.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import BatchModConfirmForm from "@/components/forms/BatchModConfirmForm";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

describe("Batch confirm, OCR Image Subtitles", () => {
  it("says what it reads and posts ocr for the selection", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post("/api/subtitles/batch", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({
          queued: 1,
          skipped: 0,
          errors: [],
          job_id: 9,
        });
      }),
    );
    const user = userEvent.setup();
    customRender(
      <BatchModConfirmForm
        items={[{ type: "movie", radarrId: 7, arr_instance_id: 2 }]}
        action="ocr"
      />,
    );

    expect(screen.getByText("OCR Image Subtitles")).toBeInTheDocument();
    expect(
      screen.getByText(
        "For each subtitle the title's language profile wants, reads its PGS or VobSub track into an SRT next to the video, unless that subtitle has a file or an embedded text track already. One track at a time; a film takes minutes.",
      ),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Apply to 1 Item(s)" }),
    );

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({
      action: "ocr",
      items: [{ type: "movie", radarrId: 7, arr_instance_id: 2 }],
    });
  });

  it("adds no note to an action whose name says it all", () => {
    customRender(
      <BatchModConfirmForm
        items={[{ type: "movie", radarrId: 7, arr_instance_id: 2 }]}
        action="extract"
      />,
    );

    expect(screen.queryByText(/PGS or VobSub/)).not.toBeInTheDocument();
  });
});
