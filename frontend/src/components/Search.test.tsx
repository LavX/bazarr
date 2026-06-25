import { http, HttpResponse } from "msw";
import { describe, it } from "vitest";
import { Search } from "@/components/index";
import { customRender } from "@/tests";
import server from "@/tests/mocks/node";

describe("Search Bar", () => {
  it("should render the closed empty state", () => {
    server.use(
      http.get("/api/system/searches", () => {
        return HttpResponse.json([]);
      }),
    );

    customRender(<Search />);
  });
});
