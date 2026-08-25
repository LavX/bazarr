# Contributing to Bazarr+

## Tools required

- Python 3.12+ (3.14 recommended, matches Docker image)
- Node.js (version in `frontend/.nvmrc`)
- Git
- Docker and Docker Compose (for integration testing)
- UI testing should be done in Chrome latest version

## Branching

### Branch model

- `master` contains stable releases, tagged with semver versions (e.g., `v2.0.0`, `v2.1.0`)
- `development` is the integration branch where upstream merges and new features land
- Feature branches are created from `development` and merged back via PR

### Rules

- `master` is not merged back to `development`
- All feature branches are branched from `development`
- Cherry-picked upstream fixes go into `development` first, never directly to `master`

## Upstream relationship

Bazarr+ is a hard fork of [upstream Bazarr](https://github.com/morpheus65535/bazarr). There is no automatic synchronization. Bug fixes from upstream may be cherry-picked selectively when relevant, but upstream releases are not merged wholesale.

## Subtitle providers go in the catalog, not here

If you are fixing or adding a **subtitle provider**, it does not belong in this repository.

Providers ship as plugins in the [Bazarr+ provider catalog](https://github.com/LavX/bazarr-provider-catalog)
and are installed at runtime through the Provider Hub. That is deliberate: a catalog fix reaches
users as a plugin release, instead of waiting for a Bazarr+ release, and a provider that breaks
cannot take the application down with it, because the Hub runs plugins out of process.

The built-in providers under `custom_libs/subliminal_patch/providers/` are **legacy**. They are
deprecated and will be removed after v3.0.0, and they are not maintained. Each portable one carries
a header saying so. A pull request that patches one will most likely be asked to move to the
catalog, which is wasted effort for you, so please start there.

One exception to the release-independence above, worth knowing before you port anything: the
Provider Hub will not let a plugin quietly take over a built-in provider id. An id has to be
listed in `bazarr/provider_hub/migration.py` before an official catalog plugin can claim it, and a
plugin claiming an unlisted id is skipped at registration. Check that file first, because adding an
id to it needs a release of this repository.

The ids kept off it are ones where the provider's site is gone, `podnapisi`, `subscenter` and
`xsubs` among them: `podnapisi.net` and `subscenter.info` no longer resolve, and `xsubs.tv` now
serves an unrelated site. There is nothing left to port for those, so please do not spend time on
one.

Read `docs/writing-a-scraper-provider.md` in the catalog repository before you begin. One thing
worth checking early: if the built-in you are porting from uses `CFSession`, the site is behind
Cloudflare, and the plugin needs cloudscraper plus the anti-captcha or FlareSolverr manifest flags
rather than the stdlib `urllib` that most plugins use.

Everything else in this repository, including the subtitle engine, scoring, the search pool, the
API, the database, and the frontend, is contributed normally. The rule is about providers only.

## Contribution workflow

1. Fork the repository
2. Create a feature branch from `development`
3. Make your changes
4. Write or update tests for your changes
5. Run linting and tests, verify they pass
6. Submit a PR targeting the `development` branch

For major changes, open an issue first to discuss the approach.

## Linting

All frontend code must pass ESLint before submitting a PR.

```bash
cd frontend

# Check for lint errors
npm run check

# Auto-fix import sorting and formatting
npx eslint --fix --ext .ts,.tsx src/
```

Fix all errors before submitting. Warnings should be addressed when practical.

## Testing

PRs should include tests when the change is testable. We use:

- **Backend:** pytest for Python tests
- **Frontend:** Vitest for React component and page tests

```bash
# Run backend tests
pytest tests/

# Run frontend tests
cd frontend
npm test

# Run a specific test file
npm test -- Translator
```

When to include tests:
- New features: add tests covering the core behavior
- Bug fixes: add a test that reproduces the bug and verifies the fix
- Refactors: ensure existing tests still pass, add tests if coverage gaps exist

When tests are optional:
- Pure styling/CSS changes
- Documentation updates
- Config file changes

## Commit messages

Use conventional commit style:

```
feat(translator): add batch retry for failed jobs
fix(ui): search field not clearing on page change
refactor(scraper): simplify response parsing
```

## Submodules

Bazarr+ includes the `ai-subtitle-translator` submodule (AI-powered subtitle translator).

Changes to it should be submitted to its repository:
- [LavX/ai-subtitle-translator](https://github.com/LavX/ai-subtitle-translator)

OpenSubtitles.org support is now a native Provider Hub plugin installed from the in-app
catalog, so the `opensubtitles-scraper` sidecar is no longer bundled here. That project
lives on standalone at [LavX/opensubtitles-scraper](https://github.com/LavX/opensubtitles-scraper).

## Running locally

```bash
# Clone with submodules
git clone --recursive https://github.com/LavX/bazarr.git
cd bazarr

# Backend
pip install -r requirements.txt
python bazarr.py --no-update --config ./config

# Frontend (separate terminal)
cd frontend
npm ci
npm start
```
