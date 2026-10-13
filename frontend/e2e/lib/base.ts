/**
 * The path the instance under test is served under, and the one place that
 * turns it into URLs: `general.base_url` of the containers the suite starts
 * (`BAZARR_E2E_BASE_PATH`), or the path of `BAZARR_E2E_URL` for an instance
 * somebody else runs. A spec never pastes the base in itself: every page and
 * API path goes through `appPath`, so the same spec runs at the root of a
 * host and under a sub-path such as `/bazarr`.
 *
 * Playwright resolves `"/settings"` against the baseURL's host, dropping any
 * path in the baseURL, and `new URL("/settings")` has no base to resolve
 * against at all. A URL built here therefore carries the base itself, and
 * resolves right against a baseURL that carries it too.
 */

/** The empty base, or "/bazarr": never a trailing slash, never a "//". */
function normalize(value: string, name: string): string {
  const trimmed = value.replace(/\/+$/, "");
  if (trimmed === "") return "";
  if (!trimmed.startsWith("/") || trimmed.includes("//")) {
    throw new Error(
      `${name} must be empty or a path such as "/bazarr", not ${JSON.stringify(value)}`,
    );
  }
  return trimmed;
}

/**
 * The base path of this run. With `BAZARR_E2E_URL` set it is that URL's own
 * path, because the instance speaks for itself; a `BAZARR_E2E_BASE_PATH` that
 * disagrees with it is refused rather than second-guessed. The value is
 * validated on every call, so a bad one fails the first URL built, and the
 * run before it, with a message that names it.
 */
export function basePath(): string {
  const external = process.env.BAZARR_E2E_URL;
  if (external) {
    const own = normalize(new URL(external).pathname, "BAZARR_E2E_URL's path");
    const explicit = process.env.BAZARR_E2E_BASE_PATH;
    if (
      explicit !== undefined &&
      normalize(explicit, "BAZARR_E2E_BASE_PATH") !== own
    ) {
      throw new Error(
        `BAZARR_E2E_BASE_PATH and BAZARR_E2E_URL disagree: the URL serves its instance under ${own || "the root"}`,
      );
    }
    return own;
  }
  return normalize(
    process.env.BAZARR_E2E_BASE_PATH ?? "",
    "BAZARR_E2E_BASE_PATH",
  );
}

/**
 * The URL of a page or API route of the app, `target` given from the app's
 * root ("/settings/connections", "/api/system/settings"), resolved against
 * the base the instance is served under.
 */
export function appPath(target: string): string {
  if (!target.startsWith("/")) {
    throw new Error(
      `appPath takes a path from the app's root, such as "/settings", not ${JSON.stringify(target)}`,
    );
  }
  return basePath() + target;
}
