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

Without docker or the image, every container spec is skipped with a message
saying which one is missing; the three `@harness` fit specs still run, because
they start no container.

## Commands

Run these from `frontend/`.

| Command                                                           | Runs                                                       |
| ----------------------------------------------------------------- | ---------------------------------------------------------- |
| `npm run e2e`                                                     | Everything except `@live`                                  |
| `npm run e2e -- --grep @wizard`                                   | One area                                                   |
| `BAZARR_E2E_BASE_PATH=/bazarr npm run e2e -- --project stateless` | The stateless project, under a sub-path                    |
| `npm run e2e:live`                                                | Everything, including specs that need the provider network |
| `npm run e2e:ui`                                                  | Playwright's UI mode, for writing and debugging a spec     |
| `npx playwright show-report e2e-report`                           | The HTML report of the last run                            |

Results go to `frontend/e2e-report/` (HTML report) and `frontend/e2e-results/`
(a screenshot and a trace of every failed test, with its console and network).
Both are ignored by git. Open a trace with
`npx playwright show-trace e2e-results/<test>/trace.zip`.

The **End-to-end tests** workflow on GitHub runs `npm run e2e` against an
image it builds from the dispatched commit, so it verifies that commit's
build, not a shipped image. It is started by hand from the Actions tab, takes
an optional grep, and uploads the report. Its release-pair input runs the
stateless project twice in the one dispatch, on the one image the dispatch
builds: first at the root of its host, then under `general.base_url=/bazarr`,
and both runs must pass. A nonempty grep is refused in release-pair mode with
an error, because a grep can select containerless tests and let both runs
pass without exercising the image. To verify a shipped image, run the same
pair locally against it:

```sh
BAZARR_E2E_IMAGE=<candidate> npm run e2e -- --project stateless
rm -rf e2e-report-root e2e-results-root
mv e2e-report e2e-report-root && mv e2e-results e2e-results-root
BAZARR_E2E_IMAGE=<candidate> BAZARR_E2E_BASE_PATH=/bazarr npm run e2e -- --project stateless
```

Move the first run's report aside between the two commands, as the workflow
does, because both runs write the same directories. Clear the old
destination directories first, as the workflow's own step does: `mv` into a
directory that still exists nests the moved report inside it, so a stale
pair's report would be read as this one's. The moved names are ignored by
git, each run's counts are then read from its own report, and each report's
metadata, written by global setup, records the image tag, the docker id of
that image and the base path the instance was served under, so the pair's
reports say which image they ran against and which of the two runs each
is.

A run counts only when its report shows the stateless tests passed, with
none skipped: with a mistyped `BAZARR_E2E_IMAGE` every container spec is
skipped while the three `@harness` fit specs still pass, and the command
still exits 0. The workflow is not part of the required checks.

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
| `@library`      | A real media file of an enabled library; skips where there is none    |
| `@sports`       | The Sports pages and an enabled Sportarr                              |
| `@auth`         | Form authentication, the session and the login lockout                |
| `@postgres`     | The PostgreSQL variant, selected by `BAZARR_E2E_POSTGRES`             |
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

Containers listen on `127.0.0.1` on the first free port from 6790 up.
Each worker scans from its own place in the range, its parallel index in,
wrapping, so workers that start at once do not all race one another for
the same first port. Each is
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
started`. Each request gets at most 10 seconds and each `docker` read 60, so
a server that takes the connection and never answers cannot hold the poll
past its cap. The API key is then read from the container's own
configuration and kept in memory. It is never printed or written anywhere.

A test gets extra time for a container boot only when it has one to wait for:
the stateful test that starts its file's container, and the stateless test
that starts the shared one or waits for another worker to. The startup
allowance covers every bounded stage of the start, each within its own
bound: the container's creation, the readiness phases (one per phase, which
a base path doubles), the key read, the save of the base, the restart that
applies it, the verification requests, the onboarding, and the port races
the start retries, two docker calls and a removal each, up to four of them,
so a start in which every stage takes its whole time still fits the one
test's budget, and a worker waiting for another worker's start waits on the
same allowance.
Removing a container can take over a minute on a busy host, so the stateful
test that replaces the previous file's container also gets time for that,
and the worker's own teardown has a 3 minute budget of its own, which is
also the bound a single removal is given: one that outlives it gives up on
what is left of it. The provider
helper in `lib/providers.ts` takes its install allowance up front and gives
it back when the providers were already in place. A test that runs with no
timeout, as under `--debug` or `--timeout 0`, is never given one. A provider
whose install failed is installed again once a run, is never enabled while
it stays failed, and the reason the backend recorded is added to the test's
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

## Running under a sub-path

An install can be served under a path: `general.base_url=/bazarr` puts every
page and API call under `/bazarr`. The suite can run its own containers that
way, and release verification runs the stateless project twice against each
candidate image: once at the root of its host and once under `/bazarr`, both
green before the image ships, because a sub-path run catches a root-relative
URL however it was written, as long as it travels through a page of the
context the fixtures hand out or through the api fixture: paths also travel
in helper arguments, identifiers, template literals and `href` attributes,
none of which a lint rule can see. The direct forms of creating a context,
a page or a browser outside the fixtures are what the lint rule rejects,
and under a base path the request factory refuses the context a spec asks
it for; a browser or a context created some other way is the one thing the
run cannot see. The release pair is two runs of the stateless project on
the same image, in this order: at the root first, then under the base path. The
workflow's release-pair input runs both in one dispatch, on the image it
builds once from the dispatched commit, and requires both results; a nonempty
grep is refused there, and each run counts only when its report shows the
stateless tests passed, with none skipped.

```sh
BAZARR_E2E_BASE_PATH=/bazarr npm run e2e -- --project stateless
```

With `BAZARR_E2E_BASE_PATH` set, every container the suite starts is saved
with `general.base_url=/bazarr` through the settings API and restarted whole
(the app reads the setting only at boot, and so does the supervisor in front
of it, which builds its proxy routes for the base at its own boot: an in-app
restart would leave the app's API reachable only at the root), and handed out
with a `baseURL` that carries the path. Every page and API URL the specs
build then resolves against it through `appPath()` in
`frontend/e2e/lib/base.ts`, which is also how a sub-path `BAZARR_E2E_URL` is
accepted again: the instance's own path is the base, and the suite resolves
everything against it.

The value is validated before anything runs: one that is not a path, or one
that disagrees with the path of a set `BAZARR_E2E_URL`, fails the run with a
message that names it. The start also proves the base took effect: after
readiness it reads the settings under the base and requires
`general.base_url` to be the configured base, requires the supervisor's own
status route to answer JSON under the base, which it only does if the
supervisor booted with the base, and requires the root no longer to answer
the API, so a save that was accepted but ignored, a supervisor that booted
without the base, or a process that keeps serving at the root, fails the run
instead of handing out a wrong instance.

Nothing rewrites a URL behind a spec's back, so a root-relative URL is
actually reached, and under a base path the suite guards it where the spec
itself writes the URL: the app answers such a URL with a redirect to the
base, and the supervisor answers some root paths with a 200 of its own, so
`api` refuses, before sending, a URL that resolves outside the base, and
Playwright's own `request` fixture is refused under a base in its favour.
The context's own `request` property is replaced with the guarded one
before the context is handed out, so a page created under any name, or the
context itself, refuses such a URL before sending it: a request context
fires no page request events, so the watch below would see none of it.
Every page of the context, including one a spec opens itself and popups,
fails the test in teardown with every request and main-frame navigation that
reached the instance outside the base, however the URL was written. A
deliberate root-relative `page.goto("/settings")` in a spec therefore fails
the sub-path run, at teardown, with the URL in the list.

The ESLint rules `bazarr-e2e/no-root-relative-url` and
`bazarr-e2e/no-request-shortcut` (`frontend/e2e/lint/`) flag the direct
forms while a spec is written: a leading-slash string or template literal as
the URL in `page.goto`, the request helpers (`api.get` and friends),
`expect(...).toHaveURL`, `page.waitForURL`, and `new URL(...)`, whose
leading-slash first argument replaces the whole path of any base it is
given, with or without one; `page.request` with `context.request`, which
send no API key and fire no page request events; and the direct creation
calls of a browser context, a browser page or a request context outside
the fixtures, matched on the method name so a renamed holder does not
slip through, with the `request`, `chromium`, `firefox` and `webkit`
imports flagged themselves, because a renamed import is the one form a
name-based match cannot see. Under a base path the request factory refuses
a context a spec asks it for at runtime, so a request context the factory
makes that slips past the rule does not run unguarded either. They leave
alone strings that are not URLs of
the app, such as `url.includes("/api/socket.io/")` or a media server's
`local_path` of `/movies`. They are an early warning: the sub-path run is
what catches a URL that slips past them, however it was written, as long
as it travels through a page of the context the fixtures hand out or the
api fixture.

## Writing a spec

1. Put it in `frontend/e2e/specs/<area>/<name>.spec.ts`.
2. Import the suite's test object, not Playwright's, and build every page
   and API URL with `appPath()` from `@e2e/lib/base`:

   ```ts
   import { expect, test } from "@e2e/fixtures";
   import { appPath } from "@e2e/lib/base";

   test.describe("history", { tag: ["@history"] }, () => {
     test("an empty install says so", async ({ page, api }) => {
       await page.goto(appPath("/history"));
       await expect(
         page.getByRole("heading", { name: "History" }),
       ).toBeVisible();
       const response = await api.get(appPath("/api/history/stats"));
       expect(response.ok()).toBe(true);
     });
   });
   ```

   `page` already points at the instance, and the same `appPath("/path")`
   call works whether that instance is served at the root of its host or
   under a sub-path. Never write a root-relative URL directly: Playwright
   resolves `"/path"` against the host, dropping the base, and the
   `bazarr-e2e/no-root-relative-url` lint rule flags the direct forms.
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
instances, `base.ts` resolves every URL against the base path the instance is
served under, `wizard.ts` walks the onboarding steps, `auth.ts` logs in,
`fit.ts` checks layout, `providers.ts` installs the recommended providers,
and `network.ts` reloads a page a host network change broke.

### Media a spec cannot make itself

A spec that needs a real media file, such as the subtitle tools menu, a manual
search or an upload, gets it from the library the instance already has
(`lib/library.ts`). A fresh container has no library at all and the suite has
no way to seed one, so no run of the suite has a library today. The read-only
tools-menu spec skips with a message saying why and runs against a real
install, which is the only run that has a library. The specs that also change
the instance or need the provider network (manual search, upload,
translation) are marked fixme with that reason, so they report themselves as
missing a run rather than reading as coverage: they turn on when the suite
grows a seeded-library run.

### The PostgreSQL variant

With `BAZARR_E2E_POSTGRES=1`, every container this suite starts boots against
a throwaway Postgres of its own, on a network of its own, so no run touches a
Postgres that matters. The image is `postgres:16-alpine` unless
`BAZARR_E2E_POSTGRES_IMAGE` names another. The `@postgres` spec proves the
variant: without the variable it skips, so an ordinary run stays on SQLite.

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

A change to `container.ts`, `shared.ts`, `providers.ts`, `network.ts`,
`guard.ts`, `base.ts` or global teardown needs a check there that fails
without it: the checks of `base.ts`, the sub-path start and the startup
budgets live in `base.test.ts` and `harness.test.ts`, the checks of the
context-level guard, the api fixture's refusal and the request property
the context's pages inherit in `guard.test.ts`, and the checks of the
lint rules, one per flagged form, live in `no-root-relative-url.test.ts`
and `no-request-shortcut.test.ts`, which run them with ESLint's own
RuleTester. `fit.ts` needs a real layout engine, so
its checks are the `@harness` specs, run with
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

`BAZARR_E2E_URL` may be the root of a host or port, such as
`https://bazarr.example.lan`, or an instance served under a path, such as
`https://example.lan/bazarr`. A path is kept, and every page and API call the
specs make resolves against it through `appPath()`, exactly as it does for the
containers the harness serves under a sub-path itself. Set
`BAZARR_E2E_BASE_PATH` only for those containers; pointed at an instance
somebody else runs, it may only agree with the URL's own path, or the run
fails with a message before anything runs.

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
away. `@live` specs need the real provider network, and a live spec is not
necessarily `@stateful`: the manual search, for example, only reads. The
exclusion stands for both tags either way. Add
`--grep-invert "@stateful|@live"` to any run against an install you care
about. Keep the credentials in your shell or a secret
store, never in a spec or a commit.
