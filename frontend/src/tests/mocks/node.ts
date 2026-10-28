import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { searchJobResultHandler } from "@/tests/discoverSearchJob";

// Default handlers. The multi-instance UI hooks (useArrInstances, used by the
// Series/Movies list + detail pages for the instance badge/filter, #156) call
// /api/system/arr-instances on render; default it to an empty list so render
// tests don't trip MSW's "error" onUnhandledRequest strategy. Individual tests
// can still override via server.use(...).
const server = setupServer(
  http.get(
    "/api/provider-hub/providers",
    () => new HttpResponse(null, { status: 503 }),
  ),
  http.get("/api/system/searches", () => HttpResponse.json([])),
  http.get("/api/system/arr-instances", () => HttpResponse.json([])),
  // The movie, episode and sports tables ask which embedded tracks are
  // hidden as soon as they render. Nothing hidden unless a test says so.
  http.get("/api/subtitles/embedded-hidden", () =>
    HttpResponse.json({ data: [] }),
  ),
  // Library pages draw a Play on control, which first asks which media
  // servers exist. None, unless a test says otherwise.
  http.get("/api/system/media-server-links/destinations", () =>
    HttpResponse.json({ data: [] }),
  ),
  // The searches the Discover tests queue finish with the answer they describe.
  searchJobResultHandler,
);

export default server;
