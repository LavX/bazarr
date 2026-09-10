import { createMemoryRouter, RouterProvider } from "react-router";
import { expect, it } from "vitest";
import { DiscoverSetupReturn, useDiscover } from "@/contexts/Discover";
import { AllProviders } from "@/providers";
import { rawRender, screen } from "@/tests";

// Every route a reader can be sent to from Discover owes them the way back to
// the exact task they left. Discover itself is the destination, so the offer
// stands down there.
//
// Coverage limit, stated rather than implied: every case in this file mounts the
// component itself, so they prove its route awareness and nothing at all about
// the wiring in App/index.tsx. If that wrapper were deleted, this file would
// still pass. The wiring is proved instead against the served build, in the
// Task 13 browser evidence, where the copy-lifecycle record leaves Discover for
// Activity and for History and returns through the rendered banner both times.
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

it.each(interrupted)("offers the return from %s", async (route) => {
  const router = open();
  await screen.findByText("Discover page");
  expect(
    screen.queryByRole("link", { name: "Return to Discover" }),
  ).not.toBeInTheDocument();
  screen.getByRole("button", { name: "Seed task" }).click();
  await router.navigate(route);
  expect(
    await screen.findByRole("link", { name: "Return to Discover" }),
  ).toHaveAttribute("href", "/discover");
});

it("keeps the return off Discover itself once a task exists", async () => {
  const router = open();
  await screen.findByText("Discover page");
  screen.getByRole("button", { name: "Seed task" }).click();
  await router.navigate("/system/tasks");
  expect(
    await screen.findByRole("link", { name: "Return to Discover" }),
  ).toBeInTheDocument();
  await router.navigate("/discover");
  await screen.findByText("Discover page");
  expect(
    screen.queryByRole("link", { name: "Return to Discover" }),
  ).not.toBeInTheDocument();
});
