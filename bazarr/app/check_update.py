# coding=utf-8

import os
import re
import logging
import json
import requests
import semver
import sys

from shutil import rmtree
from zipfile import ZipFile

from app.jobs_queue import jobs_queue
from app.get_args import args
from app.config import settings

# Fork configuration - allows overriding via environment variable
# Default: LavX/bazarr (this fork)
# To use upstream releases, set BAZARR_RELEASES_REPO to the upstream repo
RELEASES_REPO = os.environ.get('BAZARR_RELEASES_REPO', 'LavX/bazarr')

# Microservice repositories to include in releases page.
# opensubtitles-scraper is intentionally absent: it is no longer bundled with Bazarr+
# (OpenSubtitles.org ships as a native Provider Hub plugin), so its releases should not
# appear in the Bazarr+ release notes. It lives on standalone at LavX/opensubtitles-scraper.
MICROSERVICE_REPOS = [
    ('LavX/ai-subtitle-translator', 'AI Subtitle Translator'),
]


def deprecated_python_version():
    # return True if Python version is deprecated
    return sys.version_info.major == 2 or (sys.version_info.major == 3 and sys.version_info.minor < 10)


def _fetch_repo_releases(repo, label=None):
    """Fetch releases from a single GitHub repo.

    Returns the list of release dicts, or None when the fetch failed, so the
    caller can tell an empty release list from one it never got.
    """
    releases = []
    url = f'https://api.github.com/repos/{repo}/releases?per_page=100'
    try:
        logging.debug(f'BAZARR getting releases from Github: {url}')  # noqa: G004
        r = requests.get(url, allow_redirects=True, timeout=15)
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        logging.exception(f"Error trying to get releases from Github ({repo}). Http error.")  # noqa: G004
    except requests.exceptions.ConnectionError:
        logging.exception(f"Error trying to get releases from Github ({repo}). Connection Error.")  # noqa: G004
    except requests.exceptions.Timeout:
        logging.exception(f"Error trying to get releases from Github ({repo}). Timeout Error.")  # noqa: G004
    except requests.exceptions.RequestException:
        logging.exception(f"Error trying to get releases from Github ({repo}).")  # noqa: G004
    else:
        try:
            releases_data = r.json()
        except ValueError:
            logging.error(f"Error parsing JSON from Github releases response ({repo}). Skipping.")  # noqa: G004
            return None
        if not isinstance(releases_data, list):
            logging.error(f"Github releases response ({repo}) is not a list of releases. Skipping.")  # noqa: G004
            return None
        for release in releases_data:
            download_link = None
            for asset in release.get('assets', []):
                download_link = asset['browser_download_url']
                break
            entry = {'name': release['name'],
                     'body': release['body'] or '',
                     'date': release['published_at'],
                     'prerelease': release['prerelease'],
                     'download_link': download_link}
            if label:
                entry['repo'] = label
            releases.append(entry)
        logging.debug(f'BAZARR fetched {len(releases)} releases from {repo}')  # noqa: G004
        return releases
    return None


def _releases_file():
    return os.path.join(args.config_dir, 'config', 'releases.txt')


def _release_tags_file():
    return os.path.join(args.config_dir, 'config', 'release_tags.json')


def _advertised_tags(repo):
    """The tag shas the git smart-HTTP ref advertisement of ``repo`` lists.

    An annotated tag keeps its tag object's sha rather than the peeled commit,
    so a tag recreated on the same commit is still a movement.

    The advertisement is answered outside the GitHub REST quota, so reading it
    on every check costs nothing the releases API would. Returns None when it
    could not be read or parsed; the caller then fetches the releases list as
    it did before the gate existed.
    """
    try:
        from provider_hub.service import _fetch_git_ref_advertisement, _parse_git_ref_advertisement

        owner, name = repo.split('/', 1)
        refs = _parse_git_ref_advertisement(_fetch_git_ref_advertisement(owner, name), peel_tags=False)
    except Exception as e:
        logging.debug(f'BAZARR cannot read the tag advertisement of {repo}, fetching its releases: {e!r}')  # noqa: G004
        return None
    return {ref: sha for ref, sha in refs.items() if ref.startswith('refs/tags/')}


def _load_release_tags():
    """The tag state recorded at each repo's last successful releases fetch.

    Read with setdefault semantics: a missing, unreadable or malformed file is
    an empty state, which gates nothing, so a first run or an upgraded install
    fetches every repo once.
    """
    try:
        with open(_release_tags_file(), 'r', encoding='UTF-8') as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    repos = state.setdefault('repos', {})
    if not isinstance(repos, dict):
        state['repos'] = {}
    return state


def _save_release_tags(state):
    path = _release_tags_file()
    temporary = f'{path}.tmp'
    try:
        with open(temporary, 'w', encoding='UTF-8') as f:
            json.dump(state, f)
        os.replace(temporary, path)
    except OSError:
        logging.exception(f'BAZARR cannot save the release tag state to {path}')  # noqa: G004


def _cached_releases_by_label():
    """The entries of the last written releases.txt, grouped by repo label, or
    None when there is no readable file to reuse."""
    try:
        with open(_releases_file(), 'r', encoding='UTF-8') as f:
            cached = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(cached, list):
        return None
    by_label = {}
    for entry in cached:
        if not isinstance(entry, dict):
            return None
        by_label.setdefault(entry.get('repo', 'Bazarr+'), []).append(entry)
    return by_label


def check_releases(job_id=None, startup=False, wait_for_completion=False):
    # startup is used to prevent trying to create a job before the jobs queue is initialized
    if not startup and not job_id:
        jobs_queue.add_job_from_function("Updating Release Info", is_progress=False,
                                         wait_for_completion=wait_for_completion)
        return

    state = _load_release_tags()
    recorded = state['repos']
    cached = _cached_releases_by_label()

    repos = [(RELEASES_REPO, 'Bazarr+'), *MICROSERVICE_REPOS]
    # A repo no longer checked has no entries in releases.txt to reuse, and its
    # label may since belong to another repo.
    for repo in set(recorded) - {repo for repo, _label in repos}:
        recorded.pop(repo)

    releases = []
    for repo, label in repos:
        tags = _advertised_tags(repo)
        previous = recorded.get(repo)
        if (tags is not None and cached is not None and isinstance(previous, dict)
                and previous.get('tags') == tags
                and previous.get('count') == len(cached.get(label, []))):
            logging.debug(f'BAZARR tags of {repo} have not moved, reusing its last fetched releases')  # noqa: G004
            releases.extend(cached.get(label, []))
            continue

        fetched = _fetch_repo_releases(repo, label=label)
        if fetched is None:
            # Only a successful fetch may record tags. Forgetting the old ones
            # makes the next check fetch again, even if the tags move back.
            recorded.pop(repo, None)
            continue
        releases.extend(fetched)
        if tags is None:
            recorded.pop(repo, None)
        else:
            recorded[repo] = {'tags': tags, 'count': len(fetched)}

    releases.sort(key=lambda r: r['date'], reverse=True)

    with open(_releases_file(), 'w') as f:
        json.dump(releases, f)
    logging.debug(f'BAZARR saved {len(releases)} releases to releases.txt')  # noqa: G004

    _save_release_tags(state)

    if job_id:
        jobs_queue.update_job_name(job_id=job_id, new_job_name="Updated Release Info")


def check_if_new_update():
    # Skip auto-update when running from source (no BAZARR_VERSION set)
    bazarr_version = os.environ.get("BAZARR_VERSION", "")
    if not bazarr_version:
        logging.debug('BAZARR running from source, skipping auto-update')
        check_releases(startup=True)
        return

    if settings.general.branch == 'master':
        use_prerelease = False
    elif settings.general.branch == 'development':
        use_prerelease = True
    else:
        logging.error(f'BAZARR unknown branch provided to updater: {settings.general.branch}')  # noqa: G004
        return
    logging.debug(f'BAZARR updater is using {settings.general.branch} branch')  # noqa: G004

    check_releases(startup=True)

    with open(os.path.join(args.config_dir, 'config', 'releases.txt'), 'r') as f:
        data = json.load(f)
    if not args.no_update:
        release = None
        if use_prerelease:
            if deprecated_python_version():
                release = next((item['name'].lstrip('v') for item in data if
                                semver.VersionInfo.parse('1.3.1') > semver.VersionInfo.parse(item['name'].lstrip('v'))))
            else:
                release = next((item for item in data), None)
        else:
            if deprecated_python_version():
                next((item['name'].lstrip('v') for item in data if
                      not item['prerelease'] and semver.VersionInfo.parse('1.3.1') > semver.VersionInfo.parse(
                          item['name'].lstrip('v'))))
            else:
                release = next((item for item in data if not item["prerelease"]), None)

        if release and 'name' in release:
            logging.debug(f'BAZARR last release available is {release["name"]}')  # noqa: G004
            if deprecated_python_version():
                logging.warning('BAZARR is using a deprecated Python version, you must update Python to get latest '
                                'version available.')

            current_version = None
            try:
                current_version = semver.VersionInfo.parse(os.environ["BAZARR_VERSION"])
                semver.VersionInfo.parse(release['name'].lstrip('v'))
            except ValueError:
                new_version = True
            else:
                new_version = True if semver.compare(release['name'].lstrip('v'), os.environ["BAZARR_VERSION"]) > 0 \
                    else False

            # skip update process if latest release is v0.9.1.1 which is the latest pre-semver compatible release
            if new_version and release['name'] != 'v0.9.1.1':
                logging.debug(f'BAZARR newer release available and will be downloaded: {release["name"]}')  # noqa: G004
                download_release(url=release['download_link'])
            # rolling back from nightly to stable release
            elif current_version:
                if current_version.prerelease and not use_prerelease:
                    logging.debug(f'BAZARR previous stable version will be downloaded: {release["name"]}')  # noqa: G004
                    download_release(url=release['download_link'])
            else:
                logging.debug('BAZARR no newer release have been found')
        else:
            logging.debug('BAZARR no release found')
    else:
        logging.debug('BAZARR --no_update have been used as an argument')


def download_release(url):
    if not url:
        logging.debug('BAZARR release has no download URL, skipping update')
        return
    r = None
    update_dir = os.path.join(args.config_dir, 'update')
    try:
        os.makedirs(update_dir, exist_ok=True)
    except Exception:
        logging.debug(f'BAZARR unable to create update directory {update_dir}')  # noqa: G004
    else:
        logging.debug(f'BAZARR downloading release from Github: {url}')  # noqa: G004
        r = requests.get(url, allow_redirects=True, timeout=300)
    if r:
        try:
            with open(os.path.join(update_dir, 'bazarr.zip'), 'wb') as f:
                f.write(r.content)
        except Exception:
            logging.exception('BAZARR unable to download new release and save it to disk')
        else:
            apply_update()


def install_downloaded_update():
    """Install a release this instance downloaded for itself, if there is one.

    This is what startup calls, and the gate is the point of it. `args.no_update`
    is true for `--no-update` and for `updatemethod=External`, which is what the
    container sets, and an install that is updated from the outside must not
    unpack a release over its own tree. The container's tree is owned by root so
    the attempt fails anyway, noisily, halfway through; a development container
    that bind-mounts a source tree over /app/bazarr has a writable one, and there
    the attempt would succeed and overwrite the operator's checkout.

    The archive is reachable at all because /config outlives the install that
    wrote it: a directory carried over from a source install can still hold the
    release that install had queued.
    """
    if args.no_update:
        discard_stale_update()
    else:
        apply_update()


def discard_stale_update():
    """Throw away a release archive this install is never going to unpack.

    Removing it, rather than leaving it, is what keeps this a one-time event
    instead of something the operator reads in the log on every restart.
    """
    bazarr_zip = os.path.join(args.config_dir, 'update', 'bazarr.zip')
    if not os.path.isfile(bazarr_zip):
        return

    try:
        os.remove(bazarr_zip)
    except OSError:
        logging.exception(f'BAZARR cannot remove the leftover release archive {bazarr_zip}. Updates are managed '  # noqa: G004
                          'externally for this install, so it will not be installed. Delete it by hand.')
    else:
        logging.info(f'BAZARR discarded a leftover release archive ({bazarr_zip}). Updates are managed externally '  # noqa: G004
                     'for this install, so it was not unpacked.')


def apply_update():
    is_updated = False
    update_dir = os.path.join(args.config_dir, 'update')
    bazarr_zip = os.path.join(update_dir, 'bazarr.zip')
    bazarr_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    build_dir = os.path.join(bazarr_dir, 'frontend', 'build')

    if os.path.isdir(update_dir):
        if os.path.isfile(bazarr_zip):
            logging.debug(f'BAZARR is trying to unzip this release to {bazarr_dir}: {bazarr_zip}')  # noqa: G004
            try:
                with ZipFile(bazarr_zip, 'r') as archive:
                    zip_root_directory = ''
                    if len({item.split('/')[0] for item in archive.namelist()}) == 1:
                        zip_root_directory = archive.namelist()[0]

                    if os.path.isdir(build_dir):
                        try:
                            rmtree(build_dir, ignore_errors=True)
                        except Exception:
                            logging.exception(
                                'BAZARR was unable to delete the previous build directory during upgrade process.')

                    for file in archive.namelist():
                        if file.startswith(zip_root_directory) and file != zip_root_directory:
                            file_path = os.path.join(bazarr_dir, file[len(zip_root_directory):])
                            parent_dir = os.path.dirname(file_path)
                            os.makedirs(parent_dir, exist_ok=True)
                            if not os.path.isdir(file_path):
                                if os.path.exists(file_path):
                                    # remove the file first to handle case-insensitive file systems
                                    os.remove(file_path)
                                with open(file_path, 'wb+') as f:
                                    f.write(archive.read(file))
            except Exception:
                logging.exception('BAZARR unable to unzip release')
            else:
                is_updated = True
                try:
                    logging.debug('BAZARR successfully unzipped new release and will now try to delete the leftover '
                                  'files.')
                    update_cleaner(zipfile=bazarr_zip, bazarr_dir=bazarr_dir, config_dir=args.config_dir)
                except Exception:
                    logging.exception('BAZARR unable to cleanup leftover files after upgrade.')
                else:
                    logging.debug('BAZARR successfully deleted leftover files.')
            finally:
                logging.debug('BAZARR now deleting release archive')
                os.remove(bazarr_zip)
    else:
        return

    if is_updated:
        logging.debug('BAZARR new release have been installed, now we restart')
        from .server import webserver
        webserver.restart()


def update_cleaner(zipfile, bazarr_dir, config_dir):
    with ZipFile(zipfile, 'r') as archive:
        file_in_zip = archive.namelist()
    logging.debug(f'BAZARR zip file contain {len(file_in_zip)} directories and files')  # noqa: G004
    separator = os.path.sep
    if os.path.sep == '\\':
        logging.debug('BAZARR upgrade leftover cleaner is running on Windows. We\'ll fix the zip file separator '
                      'accordingly.')
        for i, item in enumerate(file_in_zip):
            file_in_zip[i] = item.replace('/', '\\')
        separator += os.path.sep
    else:
        logging.debug('BAZARR upgrade leftover cleaner is running on something else than Windows. The zip file '
                      'separator are fine.')

    dir_to_ignore = [f'^.{separator}',
                     f'^bin{separator}',
                     f'^venv{separator}',
                     f'^.venv{separator}',
                     f'^WinPython{separator}',
                     f'{separator}__pycache__{separator}$']
    if os.path.abspath(bazarr_dir).lower() == os.path.abspath(config_dir).lower():
        # for users who installed Bazarr inside the config directory (ie: `%programdata%\Bazarr` on windows)
        dir_to_ignore.append(f'^backup{separator}')
        dir_to_ignore.append(f'^cache{separator}')
        dir_to_ignore.append(f'^config{separator}')
        dir_to_ignore.append(f'^db{separator}')
        dir_to_ignore.append(f'^log{separator}')
        dir_to_ignore.append(f'^restore{separator}')
        dir_to_ignore.append(f'^update{separator}')
    elif os.path.abspath(bazarr_dir).lower() in os.path.abspath(config_dir).lower():
        # when config directory is a child of Bazarr installation directory
        dir_to_ignore.append(f'^{os.path.relpath(config_dir, bazarr_dir)}{separator}')
    dir_to_ignore_regex_string = '(?:% s)' % '|'.join(dir_to_ignore)
    logging.debug(f'BAZARR upgrade leftover cleaner will ignore directories matching this '  # noqa: G004
                  f'regex: {dir_to_ignore_regex_string}')
    dir_to_ignore_regex = re.compile(dir_to_ignore_regex_string)

    file_to_ignore = ['nssm.exe', '7za.exe', 'unins000.exe', 'unins000.dat']
    # prevent deletion of leftover Apprise.py/pyi files after 1.8.0 version that caused issue on case-insensitive
    # filesystem. This could be removed in a couple of major versions.
    file_to_ignore += ['Apprise.py', 'Apprise.pyi', 'apprise.py', 'apprise.pyi']
    logging.debug(f'BAZARR upgrade leftover cleaner will ignore those files: {", ".join(file_to_ignore)}')  # noqa: G004
    extension_to_ignore = ['.pyc']
    logging.debug(
        f'BAZARR upgrade leftover cleaner will ignore files with those extensions: {", ".join(extension_to_ignore)}')  # noqa: G004

    file_on_disk = []
    folder_list = []
    for foldername, subfolders, filenames in os.walk(bazarr_dir):
        relative_foldername = os.path.relpath(foldername, bazarr_dir) + os.path.sep

        if not dir_to_ignore_regex.findall(relative_foldername):
            if relative_foldername not in folder_list:
                folder_list.append(relative_foldername)

        for file in filenames:
            if file in file_to_ignore:
                continue
            elif os.path.splitext(file)[1] in extension_to_ignore:
                continue
            elif foldername == bazarr_dir:
                file_on_disk.append(file)
            else:
                current_dir = relative_foldername
                filepath = os.path.join(current_dir, file)
                if not dir_to_ignore_regex.findall(filepath):
                    file_on_disk.append(filepath)
    logging.debug(f'BAZARR directory contain {len(file_on_disk)} files')  # noqa: G004
    logging.debug(f'BAZARR directory contain {len(folder_list)} directories')  # noqa: G004
    file_on_disk += folder_list
    logging.debug(f'BAZARR directory contain {len(file_on_disk)} directories and files')  # noqa: G004

    file_to_remove = list(set(file_on_disk) - set(file_in_zip))
    logging.debug(f'BAZARR will delete {len(file_to_remove)} directories and files')  # noqa: G004
    logging.debug(f'BAZARR will delete this: {", ".join(file_to_remove)}')  # noqa: G004

    for file in file_to_remove:
        filepath = os.path.join(bazarr_dir, file)
        try:
            if os.path.isdir(filepath):
                rmtree(filepath, ignore_errors=True)
            else:
                os.remove(filepath)
        except Exception:
            logging.debug(f'BAZARR upgrade leftover cleaner cannot delete {filepath}')  # noqa: G004
