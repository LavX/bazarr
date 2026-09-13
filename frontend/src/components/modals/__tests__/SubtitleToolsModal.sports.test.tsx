/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import SubtitleToolsModal, {
  SportsToolsItem,
} from "@/components/modals/SubtitleToolsModal";
import { useModals } from "@/modules/modals";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const payload: SportsToolsItem[] = [
  {
    id: 61,
    arr_instance_id: 1,
    title: "Race",
    isSports: true,
    subtitles: [
      {
        code2: "en",
        name: "English",
        path: "/sports/race.en.hi.srt",
        hi: true,
        forced: false,
      },
    ],
  },
  {
    id: 62,
    arr_instance_id: 2,
    title: "Race 4K",
    isSports: true,
    subtitles: [
      {
        code2: "de",
        name: "German",
        path: "/sports/race.de.forced.srt",
        hi: false,
        forced: true,
      },
    ],
  },
];

function LaunchTools() {
  const modals = useModals();
  return (
    <button
      onClick={() => modals.openContextModal(SubtitleToolsModal, { payload })}
    >
      Open tools
    </button>
  );
}

it("opens every selected existing subtitle in an owned manual search and dismisses each once", async () => {
  const user = userEvent.setup();
  const searches: unknown[] = [];
  let automaticCalls = 0;
  server.use(
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { code2: "en", code3: "eng", name: "English", enabled: true },
        { code2: "de", code3: "deu", name: "German", enabled: true },
      ]),
    ),
    http.get("/api/system/languages/profiles", () => HttpResponse.json([])),
    http.post("/api/sports/events/:id/search", async ({ request, params }) => {
      searches.push({
        eventId: Number(params.id),
        ...((await request.json()) as object),
      });
      return HttpResponse.json({ data: [] });
    }),
    http.post("/api/sports/events/:id/automatic", () => {
      automaticCalls += 1;
      return HttpResponse.json({ queued: false });
    }),
  );
  customRender(<LaunchTools />);
  await user.click(screen.getByRole("button", { name: "Open tools" }));
  await user.click((await screen.findAllByRole("checkbox"))[0]);
  await user.click(screen.getByRole("button", { name: "Select Action" }));
  await user.click(await screen.findByText("Search"));

  await waitFor(() =>
    expect(screen.getByLabelText("Language")).toHaveValue("de"),
  );
  expect(screen.getByRole("checkbox", { name: "Forced" })).toBeChecked();
  expect(
    screen.getByRole("checkbox", { name: "Hearing impaired" }),
  ).not.toBeChecked();
  await user.click(screen.getByRole("button", { name: "Search" }));
  await waitFor(() => expect(searches).toHaveLength(1));
  await user.keyboard("{Escape}");

  await waitFor(() =>
    expect(screen.getByLabelText("Language")).toHaveValue("en"),
  );
  expect(
    screen.getByRole("checkbox", { name: "Hearing impaired" }),
  ).toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Forced" })).not.toBeChecked();
  await user.click(screen.getByRole("button", { name: "Search" }));
  await waitFor(() => expect(searches).toHaveLength(2));
  expect(searches).toEqual([
    {
      eventId: 62,
      arr_instance_id: 2,
      language: "de",
      hi: false,
      forced: true,
    },
    {
      eventId: 61,
      arr_instance_id: 1,
      language: "en",
      hi: true,
      forced: false,
    },
  ]);
  expect(automaticCalls).toBe(0);
  await user.keyboard("{Escape}");
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(screen.queryByText("Subtitle Tools")).not.toBeInTheDocument();
});
