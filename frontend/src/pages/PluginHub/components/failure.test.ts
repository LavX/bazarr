import { describe, expect, it } from "vitest";
import { explainFailure } from "./failure";

describe("the failure copy", () => {
  const cases: Array<[string, string]> = [
    [
      "ProxyError: tunnel connection failed",
      "Your proxy blocked the download.",
    ],
    [
      "The signed catalog has expired (expired). Check that this host's clock is right.",
      "The signed catalog has expired, or this host's clock is wrong.",
    ],
    [
      "The catalog signature is not valid (bad signature).",
      "The catalog's signature did not check out.",
    ],
    [
      "GitHub could not be asked: rate limited",
      "The catalog or the download could not be reached.",
    ],
    [
      "No matching distribution found for somewheel",
      "A package this plugin needs is not available for this platform.",
    ],
    [
      "Provider Hub install for demo exceeded its time budget",
      "The catalog or the download could not be reached.",
    ],
  ];
  for (const [message, cause] of cases) {
    it(`words "${message.slice(0, 40)}"`, () => {
      expect(explainFailure(message)?.cause).toBe(cause);
    });
  }

  it("keeps the server's own sentence for a requirement that cannot be met, and names it", () => {
    const message =
      "Backend needs engine.v1, which no plugin this Bazarr+ can install provides.";
    expect(explainFailure(message)).toEqual({
      cause: message,
      next: "Install the plugin that provides it first, or choose another release.",
    });
  });

  it("points at the log for a cause it does not know, and says nothing for no cause", () => {
    expect(explainFailure("Something odd happened.")).toEqual({
      cause: "Something odd happened.",
      next: "Open the log for more detail.",
    });
    expect(explainFailure("  ")).toBeNull();
    expect(explainFailure(null)).toBeNull();
  });

  it("never offers a command line", () => {
    for (const message of [
      "pip install failed: no matching distribution",
      "venv build error",
    ]) {
      expect(JSON.stringify(explainFailure(message))).not.toMatch(
        /pip install/,
      );
    }
  });
});
