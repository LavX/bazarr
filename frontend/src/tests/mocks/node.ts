import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

// Default handlers. The multi-instance UI hooks (useArrInstances, used by the
// Series/Movies list + detail pages for the instance badge/filter, #156) call
// /api/system/arr-instances on render; default it to an empty list so render
// tests don't trip MSW's "error" onUnhandledRequest strategy. Individual tests
// can still override via server.use(...).
const server = setupServer(
  http.get("/api/system/arr-instances", () => HttpResponse.json([])),
);

export default server;
