# coding=utf-8

import logging
import datetime
import semver

from requests.exceptions import JSONDecodeError, RequestException

from dogpile.cache import make_region

from app.config import settings, empty_values, get_ssl_verify
from constants import HEADERS
from sonarr.http_session import sonarr_session

region = make_region().configure('dogpile.cache.memory')


class GetSonarrInfo:
    @staticmethod
    def version():
        """
        Call system/status API endpoint and get the Sonarr version
        @return: str
        """
        sonarr_version = region.get("sonarr_version", expiration_time=datetime.timedelta(seconds=60).total_seconds())
        if sonarr_version and sonarr_version != 'unknown':
            region.set("sonarr_version", sonarr_version)
            return sonarr_version
        else:
            sonarr_version = ''
        if settings.general.use_sonarr:
            headers = {**HEADERS, "X-Api-Key": settings.sonarr.apikey}
            try:
                sv = f"{url_sonarr()}/api/v3/system/status"
                sonarr_version = sonarr_session().get(sv, timeout=int(settings.sonarr.http_timeout),
                                                      verify=get_ssl_verify('sonarr'), headers=headers).json()['version']
            except (RequestException, JSONDecodeError, KeyError):
                logging.debug('BAZARR cannot get Sonarr version')
                sonarr_version = 'unknown'
            except Exception:
                logging.debug('BAZARR cannot get Sonarr version')
                sonarr_version = 'unknown'
        logging.debug(f'BAZARR got this Sonarr version from its API: {sonarr_version}')  # noqa: G004
        region.set("sonarr_version", sonarr_version)
        return sonarr_version

    def semver(self):
        semver_version = None
        if isinstance(self.version(), str) and self.version() not in ['', 'unknown']:
            split_version = self.version().split('.')
            if len(split_version) >= 3 and all(split_version[i].isdigit() for i in range(3)):
                # Sonarr nightly/develop builds report e.g. "4.0.9.2421-develop" and
                # linuxserver images can carry "4.0.9.2421-ls123". The 4th segment is
                # the build number; the trailing channel tag is informational. Pull the
                # leading digits as the semver prerelease so the build-number comparison
                # in sync_episodes() ("4.0.9.2421" inline-episodeFile threshold) stays
                # correct, while major/minor/patch remain available for the v4 channel
                # checks in is_deprecated() / supports_signalr_core(). Dropping the build
                # number entirely (e.g. returning Version(4,0,9)) is unsafe because
                # release outranks prerelease in semver and would falsely satisfy that
                # threshold.
                prerelease = None
                if len(split_version) > 3:
                    raw = split_version[3]
                    digit_prefix = ""
                    for ch in raw:
                        if ch.isdigit():
                            digit_prefix += ch
                        else:
                            break
                    if digit_prefix:
                        prerelease = digit_prefix
                semver_version = semver.Version(*(int(part) for part in split_version[:3]), prerelease=prerelease)
        return semver_version

    def is_deprecated(self):
        """
        Call self.version() and parse the result to determine if it's a deprecated version of Sonarr.
        @return: bool
        """
        sonarr_version = self.semver()
        return sonarr_version is not None and sonarr_version.major < 4

    def supports_signalr_core(self):
        """
        Determine if Sonarr supports the SignalR Core feed used by Bazarr.
        @return: bool
        """
        sonarr_version = self.semver()
        return sonarr_version is not None and sonarr_version.major >= 4


get_sonarr_info = GetSonarrInfo()


def url_sonarr():
    if settings.sonarr.ssl:
        protocol_sonarr = "https"
    else:
        protocol_sonarr = "http"

    if settings.sonarr.base_url == '':
        settings.sonarr.base_url = "/"
    if not settings.sonarr.base_url.startswith("/"):
        settings.sonarr.base_url = f"/{settings.sonarr.base_url}"
    if settings.sonarr.base_url.endswith("/"):
        settings.sonarr.base_url = settings.sonarr.base_url[:-1]

    if settings.sonarr.port in empty_values:
        port = ""
    else:
        port = f":{settings.sonarr.port}"

    return f"{protocol_sonarr}://{settings.sonarr.ip}{port}{settings.sonarr.base_url}"


def url_api_sonarr():
    return url_sonarr() + '/api/v3/'


def sonarr_headers(apikey_sonarr):
    return {**HEADERS, "X-Api-Key": apikey_sonarr}
