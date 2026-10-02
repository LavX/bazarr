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
(a screenshot and a trace of every failed test, with its console and network).
Both are ignored by git. Open a trace with
`npx playwright show-trace e2e-results/<test>/trace.zip`.

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
| `@harness`      | Checks the harness itself on pages it writes; needs no container      |

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

Containers listen on `127.0.0.1` on the first free port from 6790 up. Each is
named `bazarr-e2e-<run>-<port>-<attempt>`, where `<run>` is the same for every
worker of one run and `<attempt>` is new for every try, and its `/config`
volume has the same name. Both carry the `bazarr-e2e` label and a
`bazarr-e2e-run=<run>` label. Two workers or two runs that pick the same port
therefore never share a name: the one that loses the port removes only what it
made itself and tries the next port.

A container is removed with its volume when its worker is done with it, and
the shared one when onboarding it fails. Global teardown then removes anything
else labelled with the run, such as the container of a worker that was killed.
If the whole run is killed, clean up that run with its id, which is in the
names:

```sh
docker ps -aq --filter label=bazarr-e2e-run=<run> | xargs -r docker rm -f -v
docker volume ls -q --filter label=bazarr-e2e-run=<run> | xargs -r docker volume rm
```

With no other run going on the machine, `label=bazarr-e2e` in place of
`label=bazarr-e2e-run=<run>` clears every run's leftovers. Volumes made before
the run label existed have no labels; `docker volume ls -q | grep '^bazarr-e2e-'`
lists them.

Readiness is polled every 5 seconds for up to 3 minutes: `/setup` answers 200,
`/api/system/settings` answers 200 or 401, and the log says `BAZARR is
started`. Each request gets at most 10 seconds and each `docker` read 15, so a
server that takes the connection and never answers cannot hold the poll past
its cap. The API key is then read from the container's own configuration and
kept in memory. It is never printed or written anywhere.

A test gets extra time for a container boot only when it has one to wait for:
the stateful test that starts its file's container, and the stateless test
that starts the shared one or waits for another worker to. Removing a
container can take over a minute on a busy host, so the stateful test that
replaces the previous file's container also gets time for that, and the
worker's own teardown has a 3 minute budget of its own. The provider helper
in `lib/providers.ts` takes its install allowance up front and gives it back
when the providers were already in place. A test that runs with no timeout,
as under `--debug` or `--timeout 0`, is never given one. A provider whose
install failed is installed again once a run, is never enabled while it stays
failed, and the reason the backend recorded is added to the test's
annotations.

### Host network changes

Whenever an interface on the host gains or loses an address, Chromium fails
every request still waiting for a connection, to `127.0.0.1` too, with
`net::ERR_NETWORK_CHANGED`. Docker does that each time any container on the
machine starts or stops: the new interface gets an IPv6 link-local address.
The app's first load asks for more scripts and styles than Chromium opens
connections to one host, so some are always waiting, and a page that loses
them stays blank. The suite's `page` therefore makes a navigation again, up to
twice, when that is what happened to it, and annotates the test with
`network changed`. Any other failure still fails the test. Requests the app
makes after it has loaded are not retried. A reload runs the app's first-load
work again, so a spec that checks something only a first load does, such as
the What's New tour, which marks the release seen as it opens, can still fail
after one; the annotation says why.

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
   page scrolls down or sideways, or any scroll container hides content.
   Every spec runs at 1920x940, full HD minus browser chrome
   (`SUITE_VIEWPORT`); a spec that needs another size sets it with
   `test.use({ viewport })`.

Helpers live in `frontend/e2e/lib/`: `container.ts` starts and stops
instances, `wizard.ts` walks the onboarding steps, `auth.ts` logs in,
`fit.ts` checks layout, `providers.ts` installs the recommended providers, and
`network.ts` reloads a page a host network change broke.

The browser runs with the `en-US` locale, so anything the app guesses from the
browser guesses the same on every machine. The wizard's languages step, for
example, arrives with English already selected.

## Checking the harness

The harness has checks of its own, in `frontend/e2e/lib/*.test.ts`. They run
under vitest in plain Node as the `e2e-harness` project, with docker and the
network stood in for, so they need neither and run in the required frontend
tests with everything else:

```sh
npx vitest run --project e2e-harness
```

A change to `container.ts`, `shared.ts`, `providers.ts`, `network.ts` or
global teardown needs a check there that fails without it. `fit.ts` needs a
real layout engine, so its checks are the `@harness` specs, run with
`npm run e2e -- --grep @harness`; they start no container either.

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

`BAZARR_E2E_URL` must be the root of a host or port, such as
`https://bazarr.example.lan`. An instance served under a path, such as
`https://example.lan/bazarr`, is refused with a message before anything runs:
the specs address every page and API call from the root, so they would test
whatever answers there instead.

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
