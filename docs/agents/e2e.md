# End-to-end tests

The suite in `frontend/e2e/` drives a real Bazarr+ in Chromium with
Playwright. Nothing is stubbed: every spec talks to a running container built
from this repository, through the same pages and API a user gets.

## Prerequisites

- Node from `frontend/.nvmrc`, and `npm ci` in `frontend/`.
- Chromium for Playwright, once per machine: `npx playwright install chromium`
  (add `--with-deps` on a fresh Linux box).
- Docker, and a Bazarr+ image. The suite uses `bazarr-atlas:local` unless
  `BAZARR_E2E_IMAGE` names another. To build one from your checkout:

  ```sh
  cd frontend && npm run build && cd ..
  docker build --tag bazarr-atlas:local .
  ```

Without docker or the image, every spec is skipped with a message saying which
one is missing.

## Commands

Run these from `frontend/`.

| Command                                 | Runs                                                       |
| --------------------------------------- | ---------------------------------------------------------- |
| `npm run e2e`                           | Everything except `@live`                                  |
| `npm run e2e -- --grep @wizard`         | One area                                                   |
| `npm run e2e:live`                      | Everything, including specs that need the provider network |
| `npm run e2e:ui`                        | Playwright's UI mode, for writing and debugging a spec     |
| `npx playwright show-report e2e-report` | The HTML report of the last run                            |

Results go to `frontend/e2e-report/` (HTML report) and `frontend/e2e-results/`
(screenshots of failures, and traces when a retry happens). Both are ignored by
git.

The **End-to-end tests** workflow on GitHub runs `npm run e2e` against an image
built from the branch. It is started by hand from the Actions tab, takes an
optional grep, and uploads the report. It is not part of the required checks.

## Tags

Tags go in the describe block or the test options, and every test carries at
least its area.

| Tag             | Meaning                                                               |
| --------------- | --------------------------------------------------------------------- |
| `@stateful`     | The spec changes the instance, so it gets a container of its own      |
| `@wizard`       | First-run onboarding                                                  |
| `@discover`     | Discover and search                                                   |
| `@status`       | System status and health                                              |
| `@jobs`         | Jobs and tasks                                                        |
| `@mediaservers` | Plex, Jellyfin, Emby and Silo connections                             |
| `@live`         | Needs the real provider network or catalog; left out of `npm run e2e` |

## Isolation

There are two Playwright projects, picked by the `@stateful` tag.

- **stateful**: each spec file gets a fresh container on a fresh volume. The
  worker holds it for the whole file and replaces it when it moves on to the
  next one, so tests in one file share an instance, in order, and files never
  do. A failing test restarts the worker, which removes its container, so the
  next test starts clean. It runs one worker today; raising `workers` for the
  project runs files in parallel without touching a spec.
- **stateless**: one container, onboarded through the API, shared by every
  stateless spec in the run and fully parallel. Use it for specs that only read
  or that clean up after themselves.

Containers are named `bazarr-e2e-<port>`, carry the `bazarr-e2e` label, listen
on `127.0.0.1` on the first free port from 6790 up, and are removed with their
volume when the run ends. If a run is killed, clean up with:

```sh
docker ps -aq --filter label=bazarr-e2e | xargs -r docker rm -f -v
docker volume ls -q | grep '^bazarr-e2e-' | xargs -r docker volume rm
```

Readiness is polled every 5 seconds for up to 3 minutes: `/setup` answers 200,
`/api/system/settings` answers 200 or 401, and the log says `BAZARR is
started`. The API key is then read from the container's own configuration and
kept in memory. It is never printed or written anywhere.

## Writing a spec

1. Put it in `frontend/e2e/specs/<area>/<name>.spec.ts`.
2. Import the suite's test object, not Playwright's:

   ```ts
   import { expect, test } from "@e2e/fixtures";

   test.describe("history", { tag: ["@history"] }, () => {
     test("an empty install says so", async ({ page, api }) => {
       await page.goto("/history");
       await expect(
         page.getByRole("heading", { name: "History" }),
       ).toBeVisible();
       const response = await api.get("/api/history/stats");
       expect(response.ok()).toBe(true);
     });
   });
   ```

   `page` already points at the instance, so `page.goto("/path")` works.
   `api` is a request context that sends the API key. `bazarr` holds the
   `baseURL`, the `apiKey` and the container `name` if you need them.

3. Add `@stateful` to the describe tags if the spec changes anything on the
   instance.
4. Keep tests small and independent of each other's leftovers. Select by role
   and label, click the way a user would, and wait on conditions with
   `expect`. No fixed sleeps. Timeouts are tight on purpose (60 s a test, 5 s
   an assertion, 10 s an action); give a slow step its own cap instead of
   raising them.
5. For layout, `expectFitsViewport(page)` from `@e2e/lib/fit` fails when the
   page scrolls or any scroll container hides content. Every spec runs at
   1920x940, full HD minus browser chrome (`SUITE_VIEWPORT`); a spec that
   needs another size sets it with `test.use({ viewport })`.

Helpers live in `frontend/e2e/lib/`: `container.ts` starts and stops
instances, `wizard.ts` walks the onboarding steps, `auth.ts` logs in, and
`fit.ts` checks layout.

The browser runs with the `en-US` locale, so anything the app guesses from the
browser guesses the same on every machine. The wizard's languages step, for
example, arrives with English already selected.

## Pointing at an existing instance

Set `BAZARR_E2E_URL` and no container is started; every spec runs against that
instance instead.

| Variable              | Meaning                                                        |
| --------------------- | -------------------------------------------------------------- |
| `BAZARR_E2E_URL`      | The instance to test, for example `https://bazarr.example.lan` |
| `BAZARR_E2E_API_KEY`  | Its API key. Required when authentication is on                |
| `BAZARR_E2E_USER`     | The login username, for an instance with form authentication   |
| `BAZARR_E2E_PASSWORD` | The login password, set together with `BAZARR_E2E_USER`        |

```sh
BAZARR_E2E_URL=https://bazarr.example.lan \
BAZARR_E2E_API_KEY=... \
BAZARR_E2E_USER=... \
BAZARR_E2E_PASSWORD=... \
npm run e2e -- --grep @status --grep-invert "@stateful|@live"
```

Without `BAZARR_E2E_API_KEY` the key is read from the instance's page, which
only works when authentication is off.

With `BAZARR_E2E_USER` set, global setup signs in through the login form once
for the whole run and every browser context starts with that session, so specs
need no login step of their own. It makes one attempt and fails the run on a
wrong password instead of trying again, because the instance locks the account
after five failures. Check the credentials before a second run.

Against a real install, only specs tagged neither `@stateful` nor `@live` are
safe: those specs only read, or clean up after themselves, and the Status spec
expects exactly the arr and media server rows the instance has configured.
`@stateful` specs change settings, add connections, install providers, restart
Bazarr and run onboarding, so point them only at an instance you can throw
away. `@live` specs need the real provider network, and every one of them is
also `@stateful`. Add `--grep-invert "@stateful|@live"` to any run against an
install you care about. Keep the credentials in your shell or a secret
store, never in a spec or a commit.
