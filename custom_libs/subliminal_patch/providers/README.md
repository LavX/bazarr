# Legacy built-in providers

Everything in this directory is deprecated and will be removed after Bazarr+ v3.0.0.

**Do not fix subtitle providers here.** Send provider work to the
[Bazarr+ provider catalog](https://github.com/LavX/bazarr-provider-catalog) instead, where providers
ship as plugins installed at runtime through the Provider Hub. A catalog fix reaches users as a
plugin release rather than waiting for a Bazarr+ release, and a provider that breaks cannot take the
application down with it, because the Hub runs plugins out of process.

`docs/writing-a-scraper-provider.md` in that repository explains how to write or port one.

## What happens to the modules here

When a built-in is found broken, in order of preference:

1. If a catalog plugin already covers it, the built-in is deleted.
2. If the provider's site is dead, the provider is deleted. Existing installs are unaffected:
   startup strips unknown ids from the enabled-providers list, and the settings page renders an
   unknown enabled id as a placeholder rather than failing.
3. Otherwise it is left as is, with an accurate note recording why, and it is not repaired.

So a broken module here is not necessarily an oversight. Several of the excluded provider test
suites in `tests/subliminal_patch/` are known-broken on purpose, and the reason is recorded next to
each exclusion.

## What is not legacy

`__init__.py`, `utils.py`, and `_agent_list.py` are shared infrastructure rather than providers, and
are maintained normally.
