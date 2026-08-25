# Legacy built-in providers

The subtitle provider modules in this directory are deprecated and will be removed after Bazarr+
v3.0.0. The shared infrastructure here is not: see [What is not legacy](#what-is-not-legacy).

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

So a broken module here is not necessarily an oversight. CI runs a short, explicit list of
`tests/subliminal_patch/` suites, named in `.github/workflows/ci.yml`. The provider suites outside
that list are not run at all, so a provider test that fails locally is not by itself evidence that
anyone intends to repair it. Before spending time on one, open an issue and ask.

The Provider Hub keeps its own record of which built-ins an official catalog plugin may replace, in
`bazarr/provider_hub/migration.py`. An id missing from it is missing on purpose, and a plugin
claiming one is skipped at registration, so check that file before porting.

The current omissions are providers whose sites are gone: `podnapisi.net` and `subscenter.info` no
longer resolve, and `xsubs.tv` now serves an unrelated site. Those are rule 2 cases, deletion, and
there is nothing left to port for any of them.

## What is not legacy

`__init__.py`, `utils.py`, and `_agent_list.py` are shared infrastructure rather than providers, and
are maintained normally.
