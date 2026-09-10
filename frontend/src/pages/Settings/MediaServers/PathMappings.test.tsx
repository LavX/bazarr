/* eslint-disable camelcase */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import SettingsConnectionsView from "@/pages/Settings/Connections";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const id = "2af88684-d7d2-4534-82bb-a6d839cc5c10";
const item = `/api/system/media-server-instances/${id}`;

afterEach(() => window.history.replaceState(null, "", "/"));

it.each(["emby", "silo"] as const)(
  "adds, edits, removes and submits controlled %s mappings as JSON with string IDs",
  async (kind) => {
    window.history.replaceState(null, "", `/#${kind}`);
    const saved: LooseObject[] = [];
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { theme: "auto", [`use_${kind}`]: true },
        }),
      ),
      http.get("/api/system/media-server-instances", () =>
        HttpResponse.json({
          data: [
            {
              id,
              kind,
              name: "Living room",
              enabled: false,
              api_key_set: true,
              url: `https://${kind}.example`,
              verify_ssl: true,
              path_mappings: [],
            },
          ],
        }),
      ),
      http.get(`${item}/status`, () =>
        HttpResponse.json({ pending: 0, state: "idle", error_code: null }),
      ),
      http.post(`${item}/libraries`, () =>
        HttpResponse.json({
          data: [
            {
              id: "0010",
              name: "Movies",
              type: "movies",
              paths: ["/media/movies"],
            },
            { id: "0007", name: "TV", type: "series", paths: ["/media/tv"] },
          ],
          error_code: null,
        }),
      ),
      http.patch(item, async ({ request }) => {
        const body = (await request.json()) as LooseObject;
        saved.push(body);
        return HttpResponse.json({
          id,
          kind,
          name: "Living room",
          enabled: false,
          api_key_set: true,
          ...body,
        });
      }),
    );
    customRender(<SettingsConnectionsView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    if (kind === "silo") {
      await user.click(
        await screen.findByRole("button", { name: "Load libraries" }),
      );
    }
    const add = await screen.findByRole("button", { name: "Add mapping" });
    await waitFor(() => expect(add).toBeEnabled());
    await user.click(add);
    const local = screen.getByLabelText("Local path 1");
    await user.type(local, "/wrong");
    await user.clear(local);
    await user.type(local, "/tv");
    await user.type(screen.getByLabelText("Server path 1"), "/media/tv");
    if (kind === "silo") {
      await user.click(
        screen.getByRole("combobox", { name: "Silo library 1" }),
      );
      await user.click(await screen.findByText("TV (series)"));
      const library = screen.getByRole("combobox", { name: "Silo library 1" });
      await user.clear(library);
      await user.type(library, "unknown-library");
      await user.tab();
    }
    await user.click(add);
    await user.click(screen.getByRole("button", { name: "Remove mapping 2" }));
    expect(screen.getByLabelText("Local path 1")).toHaveValue("/tv");
    await user.click(
      await screen.findByRole("button", { name: /Save instance/ }),
    );
    await waitFor(() => expect(saved).toHaveLength(1));
    const expected =
      kind === "silo"
        ? [{ local_path: "/tv", remote_path: "/media/tv", library_id: "0007" }]
        : [{ local_path: "/tv", remote_path: "/media/tv" }];
    expect(saved[0].path_mappings).toEqual(expected);
    expect(JSON.stringify(saved[0].path_mappings)).not.toContain(
      "[object Object]",
    );
  },
);

it("preserves saved Silo mappings while libraries are unavailable and permits removal", async () => {
  window.history.replaceState(null, "", "/#silo");
  const saved: LooseObject[] = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", use_silo: true },
      }),
    ),
    http.get("/api/system/media-server-instances", () =>
      HttpResponse.json({
        data: [
          {
            id,
            kind: "silo",
            name: "Living room",
            enabled: false,
            api_key_set: true,
            url: "https://silo.example",
            verify_ssl: true,
            path_mappings: [
              {
                local_path: "/tv",
                remote_path: "/media/tv",
                library_id: "0007",
              },
            ],
          },
        ],
      }),
    ),
    http.get(`${item}/status`, () =>
      HttpResponse.json({ pending: 0, state: "idle", error_code: null }),
    ),
    http.post(`${item}/libraries`, () =>
      HttpResponse.json({ data: [], error_code: "connection_error" }),
    ),
    http.patch(item, async ({ request }) => {
      const body = (await request.json()) as LooseObject;
      saved.push(body);
      return HttpResponse.json({
        id,
        kind: "silo",
        name: "Living room",
        enabled: false,
        api_key_set: true,
        ...body,
      });
    }),
  );
  customRender(<SettingsConnectionsView />);
  await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
  expect(await screen.findByLabelText("Local path 1")).toHaveValue("/tv");
  expect(screen.getByLabelText("Server path 1")).toHaveValue("/media/tv");
  expect(screen.getByLabelText("Local path 1")).toBeDisabled();
  expect(screen.getByRole("combobox", { name: "Silo library 1" })).toHaveValue(
    "Unavailable library (0007)",
  );
  expect(screen.getByRole("button", { name: "Add mapping" })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Load libraries" }));
  expect(
    await screen.findByText(/Could not load Silo libraries/),
  ).toBeInTheDocument();
  expect(screen.getByLabelText("Local path 1")).toHaveValue("/tv");
  await userEvent.click(
    screen.getByRole("button", { name: "Remove mapping 1" }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: /Save instance/ }),
  );
  await waitFor(() => expect(saved).toHaveLength(1));
  expect(saved[0].path_mappings).toEqual([]);
});
