/* eslint-disable @typescript-eslint/no-empty-function */

import { http } from "msw";
import { HttpResponse } from "msw";
import { vi, vitest } from "vitest";
import "@testing-library/jest-dom";
import queryClient from "@/apis/queries";
import server from "./mocks/node";

vi.mock("recharts", async () => {
  const OriginalRechartsModule = await vi.importActual("recharts");

  return {
    ...OriginalRechartsModule,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div style={{ width: "100%", height: "100%" }}>{children}</div>
    ),
  };
});

// From https://stackoverflow.com/questions/39830580/jest-test-fails-typeerror-window-matchmedia-is-not-a-function
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vitest.fn().mockImplementation((query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vitest.fn(), // Deprecated
    removeListener: vitest.fn(), // Deprecated
    addEventListener: vitest.fn(),
    removeEventListener: vitest.fn(),
    dispatchEvent: vitest.fn(),
  })),
});

// From https://github.com/mantinedev/mantine/blob/master/configuration/jest/jsdom.mocks.js
class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

window.ResizeObserver = ResizeObserver;

window.scrollTo = () => {};

// jsdom does not implement scrollIntoView; Mantine's Select schedules a
// scroll-to-selected on open, which otherwise throws as an uncaught timer.
window.HTMLElement.prototype.scrollIntoView = () => {};

beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
});

beforeEach(() => {
  server.resetHandlers();
  server.use(
    http.get("/api/system/settings", () => {
      return HttpResponse.json({
        general: {
          theme: "auto",
        },
      });
    }),
    http.get("/api/system/languages/audio", () => {
      return HttpResponse.json([]);
    }),
    http.get("/api/system/languages/profiles", () => {
      return HttpResponse.json([]);
    }),
    http.get("/api/subtitles/upgradable", () => {
      return HttpResponse.json({ movies: [], series: [] });
    }),
  );
});

afterEach(() => {
  server.resetHandlers();

  queryClient.clear();
});

afterAll(() => server.close());
