/* eslint-disable camelcase -- API fields keep their transport names. */
import { expect, it, vi } from "vitest";
import { useSportsJob } from "@/apis/hooks/sports";
import { customRender } from "@/tests";
import SportsJobFeedback from "./SportsJobFeedback";

vi.mock("@/apis/hooks/sports", () => ({
  useSportsJob: vi.fn(() => ({ data: undefined, isError: false })),
}));

it("follows the queued job in the backend run that queued it", () => {
  customRender(
    <SportsJobFeedback
      owner={42}
      queued={{
        queued: true,
        job_id: 9,
        session: "run-a",
        message: "Search queued",
      }}
    />,
  );
  expect(useSportsJob).toHaveBeenCalledWith(9, 42, "run-a");
});
