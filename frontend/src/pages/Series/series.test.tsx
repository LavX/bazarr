import { http } from "msw";
import { HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import SeriesView from ".";

describe("Series page", () => {
  beforeEach(() => {
    server.use(
      http.get("/api/series", () => {
        return HttpResponse.json({
          data: [],
        });
      }),
    );
  });

  it("should render", async () => {
    customRender(<SeriesView />);

    await waitFor(() => {
      expect(
        screen.getByPlaceholderText("Search by title..."),
      ).toBeInTheDocument();
    });
  });
});
