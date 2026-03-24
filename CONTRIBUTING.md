# Contributing to Bazarr+

## Tools required

- Python 3.12+ (3.14 recommended, matches Docker image)
- Node.js (version in `frontend/.nvmrc`)
- Git
- Docker and Docker Compose (for integration testing)
- UI testing should be done in Chrome latest version

## Branching

### Branch model

- `master` contains stable releases, tagged with `v{upstream}+{YYMMDD}` versions
- `development` is the integration branch where upstream merges and new features land
- Feature branches are created from `development` and merged back via PR

### Rules

- `master` is not merged back to `development`
- All feature branches are branched from `development`
- Upstream sync merges go into `development` first, never directly to `master`

## Upstream sync

Bazarr+ syncs with [upstream Bazarr](https://github.com/morpheus65535/bazarr) manually after major releases. Upstream merges are always done with `--no-commit --no-ff` and reviewed before committing, to avoid reintroducing removed telemetry, overwriting branding, or conflicting with fork-specific features.

Files that are always kept as the Bazarr+ version during upstream merges:
- `package_info`
- `Dockerfile`, `docker-compose.yml`
- `README.md`
- Logo and branding assets
- Any telemetry/analytics code (removed in Bazarr+)

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
- **Frontend:** Jest for React component and page tests

```bash
# Run backend tests
pytest tests/

# Run frontend tests
cd frontend
npm test

# Run a specific test file
npm test -- --testPathPattern=Translator
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

Bazarr+ includes two submodules:
- `opensubtitles-scraper` - OpenSubtitles.org web scraper service
- `ai-subtitle-translator` - AI-powered subtitle translator

Changes to these should be submitted to their respective repositories:
- [LavX/opensubtitles-scraper](https://github.com/LavX/opensubtitles-scraper)
- [LavX/ai-subtitle-translator](https://github.com/LavX/ai-subtitle-translator)

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
