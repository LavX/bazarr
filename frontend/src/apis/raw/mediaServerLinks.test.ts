/* eslint-disable camelcase */

import { isCancel } from "axios";
import { http, HttpResponse } from "msw";
import server from "@/tests/mocks/node";
import api from "./index";

it("lets go of a lookup the page no longer waits for", async () => {
  // A server that hangs holds the request for up to two of its budgets, and a
  // browser has only a few connections to Bazarr.
  let release: () => void = () => undefined;
  server.use(
    http.get(
      "/api/system/media-server-links/resolve",
      () =>
        new Promise<Response>((resolve) => {
          release = () =>
            resolve(
              HttpResponse.json({ instance_id: "p1", state: "not_found" }),
            );
        }),
    ),
  );
  const controller = new AbortController();
  const answer = api.mediaServerLinks.resolve(
    "p1",
    "movie",
    1,
    controller.signal,
  );
  controller.abort();
  try {
    const failure = await answer.then(
      () => undefined,
      (reason: unknown) => reason,
    );
    expect(isCancel(failure)).toBe(true);
  } finally {
    release();
  }
});
