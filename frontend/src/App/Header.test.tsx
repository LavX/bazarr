import { createMemoryRouter, RouterProvider } from "react-router";
import { AppShell } from "@mantine/core";
import { beforeEach, expect, it, vi } from "vitest";
import NavbarProvider from "@/contexts/Navbar";
import { AllProviders } from "@/providers";
import { act, rawRender, screen } from "@/tests";
import AppHeader from "./Header";

function mountHeader() {
  const router = createMemoryRouter(
    [
      {
        path: "/discover",
        element: (
          <NavbarProvider value={{ showed: false, show: vi.fn() }}>
            <AppShell header={{ height: 60 }}>
              <AppHeader />
            </AppShell>
          </NavbarProvider>
        ),
      },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return screen.getByRole("banner");
}

function scrollTo(y: number) {
  act(() => {
    window.scrollY = y;
    window.dispatchEvent(new Event("scroll"));
  });
}

beforeEach(() => {
  window.scrollY = 0;
});

it("keeps no surface of its own while nothing has scrolled under it", () => {
  // At the top the shell has already offset the content, so a surface here
  // only cuts the page in two and slices through the hero glow reaching up
  // into this band.
  expect(mountHeader()).toHaveAttribute("data-scrolled", "false");
});

it("takes a surface once content passes underneath", () => {
  const header = mountHeader();
  scrollTo(120);
  expect(header).toHaveAttribute("data-scrolled", "true");
});

it("gives it up again on the way back to the top", () => {
  const header = mountHeader();
  scrollTo(120);
  scrollTo(0);
  expect(header).toHaveAttribute("data-scrolled", "false");
});

it("ignores a scroll too small to put anything behind it", () => {
  // A rubber-band scroll or a one-pixel jitter must not flicker the surface.
  const header = mountHeader();
  scrollTo(3);
  expect(header).toHaveAttribute("data-scrolled", "false");
});

it("starts with a surface when the page is restored part-way down", () => {
  window.scrollY = 400;
  expect(mountHeader()).toHaveAttribute("data-scrolled", "true");
});
