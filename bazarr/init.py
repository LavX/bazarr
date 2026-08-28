# coding=utf-8

import os
import subliminal
import datetime
import time
import rarfile

from dogpile.cache.region import register_backend as register_cache_backend

from app.config import settings, configure_captcha_func, write_config
from app.get_args import args
from app.logger import configure_logging
from utilities.binaries import get_binary, BinaryNotFound
from utilities.package import package_info_path, read_package_info
from utilities.path_mappings import path_mappings
from utilities.backup import restore_from_backup

from app.database import init_db

from literals import (EXIT_CONFIG_CREATE_ERROR, ENV_BAZARR_ROOT_DIR, DIR_BACKUP, DIR_CACHE, DIR_CONFIG, DIR_DB, DIR_LOG,
                      DIR_RESTORE)
from utilities.central import make_bazarr_dir, stop_bazarr

# set start time global variable as epoch
global startTime
startTime = time.time()

# set subliminal_patch user agent
os.environ["SZ_USER_AGENT"] = f"Bazarr/{os.environ['BAZARR_VERSION']}"

# Check if args.config_dir exist
if not os.path.exists(args.config_dir):
    # Create config_dir directory tree
    try:
        os.mkdir(os.path.join(args.config_dir))
    except OSError:
        print("BAZARR The configuration directory doesn't exist and Bazarr cannot create it (permission issue?).")
        stop_bazarr(EXIT_CONFIG_CREATE_ERROR)

os.environ[ENV_BAZARR_ROOT_DIR] = os.path.join(args.config_dir)
make_bazarr_dir(DIR_BACKUP)
make_bazarr_dir(DIR_CACHE)
make_bazarr_dir(DIR_CONFIG)
make_bazarr_dir(DIR_DB)
make_bazarr_dir(DIR_LOG)
make_bazarr_dir(DIR_RESTORE)

# set subliminal_patch hearing-impaired extension to use when naming subtitles
os.environ["SZ_HI_EXTENSION"] = settings.general.hi_extension

# set anti-captcha provider and key
configure_captcha_func()

# configure logging
configure_logging(settings.general.debug or args.debug)
import logging  # noqa: E402

# restore backup if required
restore_from_backup()


# change default base_url to ''
settings.general.base_url = settings.general.base_url.rstrip('/')
write_config()

# migrate enabled_providers from comma separated string to list
if isinstance(settings.general.enabled_providers, str) and not settings.general.enabled_providers.startswith('['):
    settings.general.enabled_providers = str(settings.general.enabled_providers.split(","))
    write_config()

# Read package_info (if exists) to override some settings by package maintainers
# This file can also provide some info about the package version and author.
#
# The path used to be derived here, one directory too high for this layout, so
# in the shipped image the lookup asked for /app/package_info while the file is
# at /app/bazarr/package_info. Everything below was skipped in silence: no
# packaged version in System Status, and updatemethod=External never disabled
# in-app updates. It lives in utilities.package now so the location is asserted
# by a test instead of being taken on trust.
package_info_file = package_info_path()
if os.path.isfile(package_info_file):
    try:
        package_info = read_package_info(package_info_file)
        # package author can force a branch to follow
        if 'branch' in package_info:
            settings.general.branch = package_info['branch']
        # package author can disable update. The setting itself is applied in
        # app.get_args, which runs early enough to gate every consumer; these
        # two are published for anything outside the app that reads them.
        if package_info.get('updatemethod', '') == 'External':
            os.environ['BAZARR_UPDATE_ALLOWED'] = '0'
            os.environ['BAZARR_UPDATE_MESSAGE'] = package_info.get('updatemethodmessage', '')
        # package author can provide version and contact info
        os.environ['BAZARR_PACKAGE_VERSION'] = package_info.get('packageversion', '')
        os.environ['BAZARR_PACKAGE_AUTHOR'] = package_info.get('packageauthor', '')
    except Exception:
        pass
    else:
        write_config()

# Configure dogpile file caching for Subliminal request
register_cache_backend("subzero.cache.file", "subzero.cache_backends.file", "SZFileBackend")
subliminal.region.configure('subzero.cache.file', expiration_time=datetime.timedelta(days=30),
                            arguments={'appname': "sz_cache", 'app_cache_dir': args.config_dir},
                            replace_existing_backend=True)
subliminal.region.backend.sync()

if not os.path.exists(os.path.join(args.config_dir, 'config', 'releases.txt')):
    from app.check_update import check_releases
    check_releases(startup=True)
    logging.debug("BAZARR Created releases file")

if not os.path.exists(os.path.join(args.config_dir, 'config', 'announcements.txt')):
    from app.announcements import get_announcements_to_file
    get_announcements_to_file(startup=True)
    logging.debug("BAZARR Created announcements file")

# Clean unused settings from config
settings['general'].pop('throtteled_providers', None)
settings['general'].pop('update_restart', None)
write_config()


# Remove deprecated providers from enabled providers in config.
#
# CRITICAL: register Provider Hub plugins into the registry BEFORE the strip
# filter runs. Otherwise any plugin provider in enabled_providers gets
# silently stripped on every startup (the registry only knows about built-in
# providers at this point — plugins are registered lazily on the first
# get_providers() call, which is too late).
#
# We must also flip any staged installations to "active" first. On a
# plugin-update restart the new version is "staged" + pending_restart=True
# at this point in startup, so active_installations() would exclude it,
# register_active_provider_classes() would skip it, and the strip filter
# would drop it from enabled_providers. main.py runs activate_staged_
# installations() later, but that's already past this filter.
from subliminal_patch.extensions import provider_registry  # noqa: E402
# Register ISO 639-3 languages missing from babelfish's bundled snapshot (e.g. Montenegrin
# "cnr") before any Language() is constructed below, so providers declaring them resolve.
from languages.extra import register_extra_languages  # noqa: E402
register_extra_languages()
provider_hub_registration_ok = True
# Auto-install official-catalog versions of enabled built-in providers so the
# catalog becomes the canonical provider source. Staged here so the
# activate_staged_installations() + register_active_provider_classes() calls
# below bring them live and shadow the built-in in this same boot. Best-effort:
# failures must never prevent startup.
try:
    from provider_hub.service import autoinstall_enabled_builtins
    auto_staged = autoinstall_enabled_builtins()
    if auto_staged:
        logging.info("Provider Hub auto-installed from official catalog: %s", auto_staged)
except Exception:  # pragma: no cover - hub failures must not prevent startup
    logging.exception("Unable to auto-install Provider Hub providers on startup")
try:
    from provider_hub.service import activate_staged_installations
    activated = activate_staged_installations()
    if activated:
        logging.info("Activated staged Provider Hub installations on startup: %s", activated)
except Exception:  # pragma: no cover - hub failures must not prevent startup
    logging.exception("Unable to activate staged Provider Hub installations on startup")
try:
    from provider_hub.registry import register_active_provider_classes
    registered = register_active_provider_classes()
    if registered:
        logging.info("Registered Provider Hub plugins into provider registry: %s", registered)
except Exception:  # pragma: no cover - hub failures must not prevent startup
    provider_hub_registration_ok = False
    logging.exception("Unable to register active Provider Hub providers on startup")
existing_providers = provider_registry.names()
enabled_providers = settings.general.enabled_providers
if provider_hub_registration_ok:
    settings.general.enabled_providers = [x for x in enabled_providers if x in existing_providers]

    # Drop the config section a retired built-in provider left behind.
    #
    # A retired id has no provider class, no validators and no secret_store
    # paths left, so its section is unreachable settings: the Settings card went
    # with the provider and nothing reads the values. It is still serialized
    # into config.yaml and into every /api/system/settings response though, and
    # a credential written there by a build older than the secret store stays in
    # clear text forever, because the encrypt-at-rest walk only visits
    # registered paths. Unsetting the section is what closes that, and it is the
    # same treatment app.config already gives series_scores / movie_scores.
    #
    # Skipped for any retired id a trusted catalog plugin has adopted: Provider
    # Hub reads that plugin's credentials out of exactly this section, and the
    # registration above is what puts the id back in existing_providers.
    from provider_hub.migration import RETIRED_BUILT_IN_PROVIDER_IDS
    stale_provider_sections = sorted(
        provider_id for provider_id in RETIRED_BUILT_IN_PROVIDER_IDS
        if provider_id not in existing_providers and hasattr(settings, provider_id)
    )
    for stale_provider_section in stale_provider_sections:
        settings.unset(stale_provider_section.upper())
    if stale_provider_sections:
        logging.info("Removed leftover config sections of retired providers: %s",
                     ", ".join(stale_provider_sections))

    write_config()
else:
    logging.warning("Skipping enabled_providers cleanup because Provider Hub registration failed")


# Initialize provider_priorities if not exists
if not hasattr(settings.general, 'provider_priorities') or not settings.general.provider_priorities:
    settings.general.provider_priorities = {}
    # Set default priorities based on current order in enabled_providers
    for idx, provider in enumerate(settings.general.enabled_providers):
        settings.general.provider_priorities[provider] = (idx + 1) * 10
    write_config()


def init_binaries():
    # RAR extractors are tried in descending order of RAR5 and solid-archive
    # reliability: unrar first, then unar, then p7zip's 7z. The runtime image
    # ships unar (Debian main) and p7zip-full; unrar is picked up only where an
    # operator installed it, since it is not redistributable from Debian main.
    #
    # Every one of these is looked up with get_binary(), which returns the
    # binary from PATH when it is there, and otherwise raises BinaryNotFound
    # straight away: none of the three has an entry in binaries.json, so
    # get_binary never attempts a (read-only, failing) self-download for them.
    # Do not re-add unrar/unar/7z entries to binaries.json, that would turn a
    # cheap PATH miss into a download attempt against a read-only bin/ tree.
    try:
        exe = get_binary("unrar")
        rarfile.UNRAR_TOOL = exe
        rarfile.UNAR_TOOL = None
        rarfile.SEVENZIP_TOOL = None
        rarfile.tool_setup(unrar=True, unar=False, bsdtar=False, sevenzip=False, force=True)
    except (BinaryNotFound, rarfile.RarCannotExec):
        try:
            exe = get_binary("unar")
            rarfile.UNAR_TOOL = exe
            rarfile.UNRAR_TOOL = None
            rarfile.SEVENZIP_TOOL = None
            rarfile.tool_setup(unrar=False, unar=True, bsdtar=False, sevenzip=False, force=True)
        except (BinaryNotFound, rarfile.RarCannotExec):
            try:
                exe = get_binary("7z")
                rarfile.UNRAR_TOOL = None
                rarfile.UNAR_TOOL = None
                rarfile.SEVENZIP_TOOL = exe
                rarfile.tool_setup(unrar=False, unar=False, bsdtar=False, sevenzip=True, force=True)
            except (BinaryNotFound, rarfile.RarCannotExec):
                logging.exception("BAZARR requires a rar archive extraction utility (unrar, unar, or 7z) and none could be found.")
                raise BinaryNotFound
            else:
                logging.debug("Using 7zip from: %s", exe)
                return exe
        else:
            logging.debug("Using unar from: %s", exe)
            return exe
    else:
        logging.debug("Using UnRAR from: %s", exe)
        return exe


init_db()
init_binaries()
path_mappings.update()

# Initialize Plex OAuth configuration
from app.config import initialize_plex  # noqa: E402
initialize_plex()
