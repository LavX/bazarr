/* eslint-disable camelcase -- API field names. */
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import SourceFormModal from "./SourceFormModal";

const validationUrl = "/api/system/media-sources/validate-path";

describe("folder path validation", () => {
  it.each([
    ["relative", "not_absolute", "The folder path must be absolute."],
    ["/", "root", "The root of the filesystem cannot be a library."],
    [
      "/missing",
      "not_a_directory",
      "The folder does not exist or is not a directory.",
    ],
    ["/private", "unreadable", "Bazarr+ cannot read the folder."],
    [
      "/config",
      "config_directory",
      "The folder cannot contain or sit inside Bazarr+'s own configuration folder.",
    ],
    [
      "/media/family",
      "overlap",
      "This folder overlaps /media, a library of the media source Existing source.",
    ],
  ])("shows the %s path error while typing", async (path, code, message) => {
    const user = userEvent.setup();
    server.use(
      http.post(validationUrl, async ({ request }) => {
        expect(await request.json()).toEqual({ path, content_kind: "movie" });
        return HttpResponse.json({
          ok: false,
          errors: [{ code, message }],
          warnings: [],
        });
      }),
    );
    customRender(
      <SourceFormModal editor={{ mode: "create-source" }} onClose={vi.fn()} />,
    );
    await user.type(
      screen.getByRole("textbox", { name: "Source name" }),
      "Home videos",
    );
    const pathInput = screen.getByRole("textbox", { name: "Folder path" });
    await user.type(pathInput, path);
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(pathInput).toHaveAccessibleDescription(
      expect.stringContaining(message),
    );
    expect(screen.getByRole("button", { name: "Add source" })).toBeDisabled();
  });

  it("shows both path warnings without blocking a valid source", async () => {
    const user = userEvent.setup();
    const arrWarning = "This folder overlaps a root folder of Sonarr Home.";
    const writeWarning = "This folder is not writable.";
    let payload: unknown;
    server.use(
      http.post(validationUrl, async ({ request }) => {
        expect(await request.json()).toEqual({
          path: "/media/series",
          content_kind: "series",
        });
        return HttpResponse.json({
          ok: true,
          errors: [],
          warnings: [arrWarning, writeWarning],
        });
      }),
      http.post("/api/system/media-sources", async ({ request }) => {
        payload = await request.json();
        return HttpResponse.json({ id: 10 }, { status: 201 });
      }),
    );
    const onClose = vi.fn();
    customRender(
      <SourceFormModal editor={{ mode: "create-source" }} onClose={onClose} />,
    );
    await user.type(
      screen.getByRole("textbox", { name: "Source name" }),
      "Local series",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Content kind" }),
      "series",
    );
    await user.type(
      screen.getByRole("textbox", { name: "Folder path" }),
      "/media/series",
    );
    expect(await screen.findByText(arrWarning)).toBeInTheDocument();
    expect(screen.getByText(writeWarning)).toBeInTheDocument();
    const add = screen.getByRole("button", { name: "Add source" });
    expect(add).toBeEnabled();
    await user.click(add);
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(payload).toEqual({
      name: "Local series",
      enabled: true,
      scan_frequency_hours: 12,
      library: {
        path: "/media/series",
        content_kind: "series",
        profile_id: null,
      },
    });
  });

  it("discards a validation result for a path that has since changed", async () => {
    const user = userEvent.setup();
    const validate = vi.fn();
    server.use(
      http.post(validationUrl, async ({ request }) => {
        const payload = (await request.json()) as { path: string };
        validate(payload.path);
        if (payload.path === "/old") {
          await delay(650);
          return HttpResponse.json({
            ok: false,
            errors: [{ code: "overlap", message: "Old path error" }],
            warnings: [],
          });
        }
        return HttpResponse.json({
          ok: true,
          errors: [],
          warnings: ["Current path warning"],
        });
      }),
    );
    customRender(
      <SourceFormModal editor={{ mode: "create-source" }} onClose={vi.fn()} />,
    );
    const input = screen.getByRole("textbox", { name: "Folder path" });
    await user.type(input, "/old");
    await waitFor(() => expect(validate).toHaveBeenCalledWith("/old"));
    await user.clear(input);
    await user.type(input, "/new");
    expect(await screen.findByText("Current path warning")).toBeInTheDocument();
    expect(screen.queryByText("Old path error")).not.toBeInTheDocument();
  });
});
