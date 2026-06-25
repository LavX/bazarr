import { http, HttpResponse } from "msw";
import { describe, it } from "vitest";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import Authentication from "./Authentication";

describe("Authentication", () => {
  it("should render without crash", () => {
    server.use(
      http.get("/system/backdrops", () => {
        return HttpResponse.json({ backdrops: [] });
      }),
    );

    customRender(<Authentication></Authentication>);

    expect(screen.getByPlaceholderText("Username")).toBeDefined();
    expect(screen.getByPlaceholderText("Password")).toBeDefined();
    expect(screen.getByRole("button", { name: "Login" })).toBeDefined();
  });

  it("renders library backdrops returned by the backend", async () => {
    server.use(
      http.get("/system/backdrops", () => {
        return HttpResponse.json({
          backdrops: ["/system/backdrop/series-1", "/system/backdrop/movies-2"],
        });
      }),
    );

    customRender(<Authentication></Authentication>);

    await waitFor(() => {
      expect(screen.getAllByTestId("login-backdrop")).toHaveLength(2);
    });
  });
});
