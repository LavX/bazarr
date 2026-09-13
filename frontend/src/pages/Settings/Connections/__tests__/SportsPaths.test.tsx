/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it, vi } from "vitest";
import InstanceFormModal from "@/pages/Settings/Connections/InstanceFormModal";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { sportarr } from "./fixtures";

it("edits and removes instance path mappings in the actual save request", async () => {
  const user = userEvent.setup();
  let saved: unknown;
  server.use(
    http.patch("/api/system/arr-instances/42", async ({ request }) => {
      saved = await request.json();
      return HttpResponse.json(sportarr);
    }),
  );
  customRender(
    <InstanceFormModal
      opened
      kind="sportarr"
      instance={{ ...sportarr, path_mappings: [["/sports", "/local"]] }}
      onClose={vi.fn()}
    />,
  );
  const local = await screen.findByRole("textbox", { name: "Bazarr path 1" });
  expect(local).toHaveValue("/local");
  await user.clear(local);
  await user.type(local, "/owner-a");
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() =>
    expect(saved).toMatchObject({ path_mappings: [["/sports", "/owner-a"]] }),
  );
  await user.click(screen.getByRole("button", { name: "Remove mapping 1" }));
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(saved).toMatchObject({ path_mappings: [] }));
});
