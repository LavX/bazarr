/* eslint-disable camelcase -- API fixtures use the server's field names. */
/**
 * The tools menu's translate-from items are user-pressed translate actions:
 * they call the mutation directly, so a closed gate disables them with its
 * reason instead of letting the click fail invisibly.
 *
 * The gate answer is awaited before the menu opens and the items are read in
 * one snapshot: Mantine's dropdown closes itself a few hundred milliseconds
 * after opening in jsdom, which races any test that queries one item after
 * another.
 */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import { cleanup, customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const missingLanguage: Subtitle = {
  code2: "hu",
  name: "Hungarian",
  forced: false,
  hi: false,
  path: null,
};

// One file source and one embedded track, so both label shapes are covered.
const translationSources: Subtitle[] = [
  {
    code2: "en",
    name: "English",
    forced: false,
    hi: false,
    path: "/movies/m.en.srt",
  },
  { code2: "de", name: "German", forced: false, hi: false, path: null },
];

function renderMenu() {
  customRender(
    <SubtitleToolsMenu
      selections={[]}
      missingLanguage={missingLanguage}
      translationSources={translationSources}
      mediaId={9}
      mediaType="movie"
      arrInstanceId={3}
    >
      <button type="button">Tools</button>
    </SubtitleToolsMenu>,
  );
}

function openTheMenu(user: ReturnType<typeof userEvent.setup>) {
  return user.click(screen.getByRole("button", { name: "Tools" }));
}

async function renderMenuWithTheGateAnswered(
  user: ReturnType<typeof userEvent.setup>,
) {
  renderMenu();
  // The hook treats an unanswered status as available, so the answer has to
  // be in the cache before the menu opens for the items to render with it.
  await waitFor(() =>
    expect(
      queryClient.getQueryData([QueryKeys.System, "status"]),
    ).toBeDefined(),
  );
  await openTheMenu(user);
}

describe("SubtitleToolsMenu translate-from items and the gate", () => {
  it("disables the items with the reason when the gate is closed", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/system/status", () =>
        HttpResponse.json({
          data: {
            translation_availability: {
              available: false,
              reason: "No translator is configured",
            },
          },
        }),
      ),
    );
    await renderMenuWithTheGateAnswered(user);

    const items = await screen.findAllByRole("menuitem");
    const fileItem = items.find((el) =>
      el.textContent?.startsWith("Translate from English"),
    );
    expect(fileItem).toBeDisabled();
    expect(fileItem).toHaveTextContent("No translator is configured");
    const embeddedItem = items.find((el) =>
      el.textContent?.startsWith("Translate from German (embedded)"),
    );
    expect(embeddedItem).toBeDisabled();
    expect(embeddedItem).toHaveTextContent("No translator is configured");
  });

  it("keeps the items enabled and queues the translation when the gate is open", async () => {
    const user = userEvent.setup();
    const patched: (string | null)[] = [];
    server.use(
      http.get("/api/system/status", () =>
        HttpResponse.json({
          data: {
            translation_availability: { available: true, reason: "" },
          },
        }),
      ),
      http.patch("/api/subtitles", ({ request }) => {
        // The form travels form-encoded, so the action is read from the URL,
        // the way the endpoint's own parser does.
        patched.push(new URL(request.url).searchParams.get("action"));
        return new HttpResponse(null, { status: 204 });
      }),
    );
    await renderMenuWithTheGateAnswered(user);

    const items = await screen.findAllByRole("menuitem");
    const fileItem = items.find(
      (el) => el.textContent === "Translate from English",
    );
    expect(fileItem).toBeEnabled();
    await user.click(fileItem!);

    await waitFor(() => expect(patched).toHaveLength(1));
    expect(patched[0]).toBe("translate");
  });

  it("shows the refusal's reason and re-reads the gate when a stale answer said open", async () => {
    // The gate answer the items read is a cached status, and the gate can
    // close elsewhere after it was read: the click has to answer with the
    // server's own reason, and the next status answer has to disable the
    // item that the stale one left clickable.
    const user = userEvent.setup();
    // The first answer is what the stale control read; every answer after it
    // says the gate has closed, including the refetch the refusal triggers.
    let statusFetches = 0;
    server.use(
      http.get("/api/system/status", () => {
        statusFetches += 1;
        return HttpResponse.json({
          data:
            statusFetches === 1
              ? { translation_availability: { available: true, reason: "" } }
              : {
                  translation_availability: {
                    available: false,
                    reason: "No translator is configured",
                  },
                },
        });
      }),
      // The subtitles endpoint answers the gate's refusal with a bare
      // sentence and 409, which is the body the refusal reads.
      http.patch(
        "/api/subtitles",
        () => new HttpResponse("No translator is configured", { status: 409 }),
      ),
    );
    await renderMenuWithTheGateAnswered(user);

    const items = await screen.findAllByRole("menuitem");
    const fileItem = items.find(
      (el) => el.textContent === "Translate from English",
    );
    await user.click(fileItem!);

    // The refusal speaks for itself: the shared client leaves a 409 to the
    // caller, and before this fix the rejection was uncaught and silent.
    expect(
      await screen.findByText("No translator is configured"),
    ).toBeInTheDocument();

    // The refusal also refetches the status, so the item reads the closed
    // gate and disables itself with the reason as its label suffix.
    await waitFor(() =>
      expect(
        queryClient.getQueryData([QueryKeys.System, "status"]),
      ).toMatchObject({
        translation_availability: { available: false },
      }),
    );
    // A fresh menu reads the now-closed gate from the refetched status, and
    // its items are read the way the other tests read them.
    cleanup();
    await renderMenuWithTheGateAnswered(user);
    const reopened = await screen.findAllByRole("menuitem");
    const refusedItem = reopened.find((el) =>
      el.textContent?.startsWith("Translate from English"),
    );
    expect(refusedItem).toBeDisabled();
    expect(refusedItem).toHaveTextContent("No translator is configured");
  });
});
