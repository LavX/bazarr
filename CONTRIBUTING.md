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
