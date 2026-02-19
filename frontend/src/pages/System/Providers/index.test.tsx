import { http } from "msw";
import { HttpResponse } from "msw";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import SystemProvidersView from ".";

describe("System Providers", () => {
  it("should render with providers", async () => {
    server.use(
      http.get("/api/providers", () => {
        return HttpResponse.json({
          data: [{ name: "Addic7ed", status: "disabled", retry: "1" }],
        });
      }),
    );

    customRender(<SystemProvidersView />);

    await waitFor(() => {
      expect(screen.getByText("Addic7ed")).toBeInTheDocument();
    });

    expect(screen.getByText("Addic7ed")).toBeInTheDocument();

    expect(screen.getByText("Name")).toBeInTheDocument();
    expect(screen.getByText("Status")).toBeInTheDocument();
    expect(screen.getByText("Next Retry")).toBeInTheDocument();

    // Verify toolbar buttons are present
    expect(screen.getByText("Refresh")).toBeInTheDocument();
    expect(screen.getByText("Reset")).toBeInTheDocument();
  });

  it("should render with no providers", async () => {
    server.use(
      http.get("/api/providers", () => {
        return HttpResponse.json({
          data: [],
        });
      }),
    );

    customRender(<SystemProvidersView />);
  });
});
