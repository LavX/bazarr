/* eslint-disable camelcase -- API fixture fields. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import Layout from "@/pages/Settings/components/Layout";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import LogFiles from "./LogFiles";

let submitted: FormData[] = [];

beforeEach(() => {
  submitted = [];
  server.use(
    // A real settings answer always carries general; the client refuses one without it.
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        log: { max_file_size_mb: 32, backup_count: 7 },
      }),
    ),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.post("/api/system/settings", async ({ request }) => {
      submitted.push(await request.formData());
      return new HttpResponse(null, { status: 204 });
    }),
  );
});

function renderLogFiles() {
  customRender(
    <Layout name="General">
      <LogFiles />
    </Layout>,
  );
}

describe("LogFiles", () => {
  it("shows the saved limits and the folder ceiling they add up to", async () => {
    renderLogFiles();

    const size = await screen.findByLabelText("Maximum Log File Size (MB)");
    await waitFor(() => expect(size).toHaveValue("32"));
    expect(screen.getByLabelText("Log Files to Keep")).toHaveValue("7");
    // The live file plus seven rolled ones, at 32 MB each.
    expect(
      screen.getByText(/use at most about 256 MB: the current file plus 7/),
    ).toBeInTheDocument();
  });

  it("saves the typed size and count", async () => {
    renderLogFiles();
    const user = userEvent.setup();

    const size = await screen.findByLabelText("Maximum Log File Size (MB)");
    await waitFor(() => expect(size).toHaveValue("32"));
    await user.clear(size);
    await user.type(size, "64");
    const count = screen.getByLabelText("Log Files to Keep");
    await user.clear(count);
    await user.type(count, "14");

    expect(
      screen.getByText(/use at most about 960 MB: the current file plus 14/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Save 2 pending/ }));

    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0].get("settings-log-max_file_size_mb")).toBe("64");
    expect(submitted[0].get("settings-log-backup_count")).toBe("14");
  });

  it("holds a typed size inside the bounds the backend accepts", async () => {
    renderLogFiles();
    const user = userEvent.setup();

    const size = await screen.findByLabelText("Maximum Log File Size (MB)");
    await waitFor(() => expect(size).toHaveValue("32"));
    await user.clear(size);
    await user.type(size, "5000");
    await user.tab();

    await waitFor(() => expect(size).toHaveValue("1024"));
    await user.click(screen.getByRole("button", { name: /Save 1 pending/ }));

    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0].get("settings-log-max_file_size_mb")).toBe("1024");
  });

  it("takes whole numbers only, since the backend refuses a fraction", async () => {
    renderLogFiles();
    const user = userEvent.setup();

    const count = await screen.findByLabelText("Log Files to Keep");
    await waitFor(() => expect(count).toHaveValue("7"));
    await user.clear(count);
    await user.type(count, "2.5");

    expect(count).toHaveValue("25");
    await user.click(screen.getByRole("button", { name: /Save 1 pending/ }));

    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0].get("settings-log-backup_count")).toBe("25");
  });
});
