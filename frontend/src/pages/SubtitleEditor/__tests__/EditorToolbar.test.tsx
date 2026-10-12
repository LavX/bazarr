/* eslint-disable camelcase -- the status fixture uses the server's field name. */
/**
 * The toolbar's AI icon reads the editor's own gate answer. A natively
 * disabled button never fires the pointer events a tooltip needs, so the
 * closed gate travels as data-disabled with a guarded click: the reason has
 * to still show, and the click has to do nothing.
 */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import EditorToolbar from "@/pages/SubtitleEditor/EditorToolbar";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

function status(availability: { available: boolean; reason: string }) {
  server.use(
    http.get("/api/system/status", () =>
      HttpResponse.json({
        data: { editor_translation_availability: availability },
      }),
    ),
  );
}

async function renderToolbar(onAction: (action: string) => void) {
  customRender(
    <EditorToolbar
      onAction={onAction}
      dirty={false}
      canUndo={false}
      canRedo={false}
      format="srt"
      language="en"
    />,
  );
  // The hook treats an unanswered status as available, so the answer has to
  // be in the cache before the icon's state says anything.
  await waitFor(() =>
    expect(
      queryClient.getQueryData([QueryKeys.System, "status"]),
    ).toBeDefined(),
  );
}

describe("EditorToolbar and the translation gate", () => {
  it("keeps the translate icon working when the gate is open", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    status({ available: true, reason: "" });
    await renderToolbar(onAction);

    await user.click(screen.getByRole("button", { name: "AI Translate" }));

    expect(onAction).toHaveBeenCalledWith("translate");
  });

  it("shows the reason and refuses the click when the gate is closed", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    status({ available: false, reason: "No translator is configured" });
    await renderToolbar(onAction);

    const icon = screen.getByRole("button", { name: "AI Translate" });
    expect(icon).toHaveAttribute("data-disabled");

    await user.hover(icon);
    expect(
      await screen.findByText("No translator is configured"),
    ).toBeInTheDocument();

    await user.click(icon);
    expect(onAction).not.toHaveBeenCalled();
  });
});
