import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import { FileBrowser } from "@/components/inputs/FileBrowser";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

it("lists directories on the Sportarr side of a sports mapping", async () => {
  let browsedPath = "";
  server.use(
    http.get("/api/files/sportarr", ({ request }) => {
      browsedPath = new URL(request.url).searchParams.get("path") ?? "";
      return HttpResponse.json([
        { name: "sports", children: true, path: "/sports/" },
        { name: "soccer", children: true, path: "/soccer/" },
      ]);
    }),
  );
  customRender(<FileBrowser type="sportarr" />);
  await userEvent.click(screen.getByPlaceholderText("Click to start"));
  expect(await screen.findByText("/sports/")).toBeInTheDocument();
  expect(await screen.findByText("/soccer/")).toBeInTheDocument();
  // The initial listing is a plain empty-path browse: the backend seeds it
  // with the synced root folders for the default Sportarr instance.
  await waitFor(() => expect(browsedPath).toBe(""));
});
