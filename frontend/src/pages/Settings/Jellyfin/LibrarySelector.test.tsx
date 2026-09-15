/* eslint-disable camelcase */
import { useState } from "react";
import { MantineProvider } from "@mantine/core";
import { useForm } from "@mantine/form";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { FormContext, FormValues } from "@/pages/Settings/utilities/FormValues";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import LibrarySelector, { LibrarySelectorProps } from "./LibrarySelector";

const libraries = [
  { id: "movies", name: "Movies", type: "movies" },
  { id: "series", name: "Series", type: "tvshows" },
  { id: "mixed", name: "Mixed", type: "" },
  { id: "home", name: "Home videos", type: "homevideos" },
  { id: "untyped", name: "Untyped", type: "" },
];

function Harness({ first }: { first: LibrarySelectorProps["libraryType"] }) {
  const [mode, setMode] = useState(first);
  const form = useForm<FormValues>({
    initialValues: {
      hooks: {},
      settings: {
        "settings-jellyfin-url": "http://fixture",
        "settings-jellyfin-apikey": "fixture",
        "settings-jellyfin-verify_ssl": true,
        "settings-jellyfin-sports_library": [],
        "settings-jellyfin-sports_library_ids": [],
      },
    },
  });
  return (
    <MantineProvider env="test">
      <FormContext.Provider value={form}>
        {(["movies", "all", "tvshows"] as const).map((value) => (
          <button key={value} onClick={() => setMode(value)}>
            {value}
          </button>
        ))}
        <LibrarySelector
          label="Libraries"
          libraryType={mode}
          settingKey={`settings-jellyfin-${mode === "all" ? "sports" : mode}_library`}
          settingKeyIds={`settings-jellyfin-${mode === "all" ? "sports" : mode}_library_ids`}
        />
        <output aria-label="Selected sports names">
          {JSON.stringify(
            form.values.settings["settings-jellyfin-sports_library"],
          )}
        </output>
        <output aria-label="Selected sports IDs">
          {JSON.stringify(
            form.values.settings["settings-jellyfin-sports_library_ids"],
          )}
        </output>
      </FormContext.Provider>
    </MantineProvider>
  );
}

it.each(["movies", "all"] as const)(
  "selects untyped Sports libraries with independent cache modes, starting with %s",
  async (first) => {
    const requests: boolean[] = [];
    server.use(
      http.post("/api/jellyfin/libraries", async ({ request }) => {
        const body = await request.formData();
        const includeAll = body.get("include_all") === "true";
        requests.push(includeAll);
        // This is the backend endpoint's verified response contract.
        return HttpResponse.json({
          data: includeAll ? libraries : libraries.slice(0, 2),
          error_code: null,
        });
      }),
    );
    const user = userEvent.setup();
    customRender(<Harness first={first} />);
    await waitFor(() => expect(requests).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "all" }));
    await user.click(screen.getByRole("combobox", { name: "Libraries" }));
    for (const name of ["Mixed", "Home videos", "Untyped"]) {
      await user.click(await screen.findByRole("option", { name }));
    }
    expect(
      screen.getByRole("status", { name: "Selected sports names" }),
    ).toHaveTextContent('["Mixed","Home videos","Untyped"]');
    expect(
      screen.getByRole("status", { name: "Selected sports IDs" }),
    ).toHaveTextContent('["mixed","home","untyped"]');
    await user.click(screen.getByRole("button", { name: "movies" }));
    await user.click(screen.getByRole("combobox", { name: "Libraries" }));
    expect(await screen.findByRole("option", { name: "Movies" })).toBeVisible();
    expect(
      screen.queryByRole("option", { name: "Mixed" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("option", { name: "Series" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "tvshows" }));
    await user.click(screen.getByRole("combobox", { name: "Libraries" }));
    expect(await screen.findByRole("option", { name: "Series" })).toBeVisible();
    expect(
      screen.queryByRole("option", { name: "Movies" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("option", { name: "Mixed" }),
    ).not.toBeInTheDocument();
    expect(requests).toEqual(
      first === "movies" ? [false, true] : [true, false],
    );
  },
);
