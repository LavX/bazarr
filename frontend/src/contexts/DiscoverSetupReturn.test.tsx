import { createMemoryRouter, RouterProvider } from "react-router";
import { expect, it } from "vitest";
import { DiscoverSetupReturn, useDiscover } from "@/contexts/Discover";
import { AllProviders } from "@/providers";
import { rawRender, screen } from "@/tests";

// Saved Discover drafts must not add return banners to unrelated pages.
const interrupted = [
  "/system/tasks",
  "/history/series",
  "/wanted/movies",
  "/settings/connections",
  "/system/providers",
  "/movies/12",
];

function Seed() {
  const { updateDraft } = useDiscover();
  return (
    <button type="button" onClick={() => updateDraft({ imdbId: "tt0133093" })}>
      Seed task
    </button>
  );
}

function open() {
  const router = createMemoryRouter(
    [
      {
        path: "/discover",
        element: (
          <DiscoverSetupReturn>
            <Seed />
            <div>Discover page</div>
          </DiscoverSetupReturn>
        ),
      },
      ...[...interrupted].map((route) => ({
        path: route,
        element: (
          <DiscoverSetupReturn>
            <div>Interruption</div>
          </DiscoverSetupReturn>
        ),
      })),
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return router;
}

it.each(interrupted)(
  "keeps %s free of Discover return banners",
  async (route) => {
    const router = open();
    await screen.findByText("Discover page");
    expect(
      screen.queryByRole("link", { name: "Return to Discover" }),
    ).not.toBeInTheDocument();
    screen.getByRole("button", { name: "Seed task" }).click();
    await router.navigate(route);
    expect(
      screen.queryByRole("link", { name: "Return to Discover" }),
    ).not.toBeInTheDocument();
  },
);

it("keeps the return off Discover itself once a task exists", async () => {
  const router = open();
  await screen.findByText("Discover page");
  screen.getByRole("button", { name: "Seed task" }).click();
  await router.navigate("/system/tasks");
  expect(
    screen.queryByRole("link", { name: "Return to Discover" }),
  ).not.toBeInTheDocument();
  await router.navigate("/discover");
  await screen.findByText("Discover page");
  expect(
    screen.queryByRole("link", { name: "Return to Discover" }),
  ).not.toBeInTheDocument();
});
