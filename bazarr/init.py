# coding=utf-8

import os
import subliminal
import datetime
import time
import rarfile

from dogpile.cache.region import register_backend as register_cache_backend

from app.config import (settings, configure_captcha_func, finish_backup_restore_startup, log_startup_notices,
                        open_database_after_backup_restore, write_config)
from app.get_args import args
from app.logger import configure_logging
from utilities.binaries import get_binary, BinaryNotFound
from utilities.package import package_info_path, read_package_info
from utilities.path_mappings import path_mappings
from utilities.backup import apply_pending_plugin_restore, restore_from_backup

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

# numpy needs SSE4.2 and POPCNT. Say so in the log once, before anything imports it.
from utilities.cpu_features import log_cpu_check  # noqa: E402
log_cpu_check()

# What reading the configuration changed in what was stored. It was found before there was a log to say it in.
log_startup_notices()

# Mark workers started during initialization as well as those started after it.
try:
    from provider_hub.sandbox import sweep as sandbox_sweep
    sandbox_sweep.configure_owner(args.config_dir)
except Exception:
    logging.exception("BAZARR cannot mark this run's plugin workers")

# restore backup if required. A restore that leaves the database unfit to run
# on (pg_restore failing partway, or the restored configuration not going in)
# stops the start from inside, and so does a dump parked by an earlier failure.
if not restore_from_backup():
    open_database_after_backup_restore()
    apply_pending_plugin_restore()
    finish_backup_restore_startup()


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


# Register ISO 639-3 languages missing from babelfish's bundled snapshot (e.g. Montenegrin
# "cnr") before any Language() is constructed below, so providers declaring them resolve.
from languages.extra import register_extra_languages  # noqa: E402
register_extra_languages()

try:
    # The translator kind exists only while the translation runtime switch is on. Registered here, ahead of
    # anything that stages or activates a plugin, and not when the kinds package loads, because reading the
    # switch needs the translation modules, which cannot load that early.
    from provider_hub.kinds.translator import register_when_enabled
    register_when_enabled()
except Exception:  # pragma: no cover - a dark feature must not prevent startup
    logging.exception("Unable to register the translator plugin kind on startup")


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
try:
    # The translator secrets live in the database, which is ready only now, and the jobs queue is in memory, so a
    # translation the last run was in the middle of lost its place: the runtime registers the secrets for log
    # scrubbing and marks those jobs to be retried, with their checkpoints kept. Nothing happens with the switch
    # off. On the first start after an upgrade the runtime's tables are not there yet: main.py runs the same call
    # once the migrations are done.
    if getattr(settings.translator, "runtime_v2", False) is True:
        from subtitles.tools.translate.runtime import run_startup_tasks
        run_startup_tasks()
except Exception:  # pragma: no cover - must not prevent startup
    logging.exception("Unable to register the translator secrets and recover interrupted translations on startup")
try:
    # The dispatcher reaches translator plugins through the worker pool once it is bound to it. Nothing is
    # bound, and nothing here is imported, with the switch off. The binding reads no table, so it does not wait
    # for the migrations.
    if getattr(settings.translator, "runtime_v2", False) is True:
        from subtitles.tools.translate.runtime import worker_binding
        worker_binding.bind_when_enabled()
except Exception:  # pragma: no cover - a dark feature must not prevent startup
    logging.exception("Unable to bind the translation runtime to its workers on startup")
init_binaries()
path_mappings.update()

# Initialize Plex OAuth configuration
from app.config import initialize_plex  # noqa: E402
initialize_plex()
