/* eslint-disable camelcase -- API fixtures use the server's field names. */
/**
 * A series combine is queued as one job. The form hands it over and closes
 * without a result toast of its own, and the series tables refresh when the
 * job finishes, not when it is queued.
 */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import CombineForm from "@/components/forms/CombineForm";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

async function pickLanguage(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
) {
  const input = screen.getByPlaceholderText("Add language");
  await user.click(input);
  // Each chosen language keeps its own Select, so pick from the listbox this
  // input controls rather than from every option on the page.
  const listbox = await waitFor(() => {
    const id = input.getAttribute("aria-controls");
    const found = screen
      .getAllByRole("listbox", { hidden: true })
      .find((element) => element.id === id);
    if (!found) throw new Error("the language list did not open");
    return found;
  });
  await user.click(
    within(listbox).getByRole("option", { name: label, hidden: true }),
  );
}

describe("CombineForm, series scope", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("queues the combine and refreshes the series on the job's terminal event", async () => {
    const posted: unknown[] = [];
    server.use(
      http.post("/api/series/5/subtitles/combine", async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json(
          { status: "queued", job_id: 44 },
          {
            status: 202,
          },
        );
      }),
    );
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const onSubmit = vi.fn();
    const user = userEvent.setup();

    customRender(
      <CombineForm
        scope={{ kind: "series", seriesId: 5 }}
        availableLanguages={["en", "hu"]}
        onSubmit={onSubmit}
      />,
    );

    await pickLanguage(user, "EN");
    await pickLanguage(user, "HU");
    await user.click(screen.getByRole("button", { name: "Generate" }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(posted).toEqual([{ languages: ["en", "hu"], format: "srt" }]);
    // The job reports its own outcome; the form adds no summary toast.
    expect(screen.queryByText(/combine complete/i)).not.toBeInTheDocument();

    const seriesRefreshes = () =>
      invalidate.mock.calls.filter(
        ([filters]) =>
          (filters as { queryKey?: unknown[] })?.queryKey?.[0] ===
          QueryKeys.Series,
      ).length;
    expect(seriesRefreshes()).toBe(0);

    queryClient.setQueryData(
      [QueryKeys.System, QueryKeys.Jobs],
      [{ job_id: 44, status: "running" }],
    );
    expect(seriesRefreshes()).toBe(0);

    queryClient.setQueryData(
      [QueryKeys.System, QueryKeys.Jobs],
      [{ job_id: 44, status: "failed" }],
    );
    expect(seriesRefreshes()).toBe(1);
  });
});
