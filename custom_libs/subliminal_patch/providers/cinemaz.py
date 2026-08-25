# LEGACY PROVIDER. Do not fix subtitle providers here.
#
# Built-in providers are deprecated and will be removed after Bazarr+ v3.0.0.
# Providers now ship as plugins in the Bazarr+ provider catalog and are
# installed at runtime through the Provider Hub, so a fix reaches users as a
# plugin release instead of waiting for a Bazarr+ release, and a broken
# provider can no longer take the application down with it.
#
# Send provider fixes here instead:
#   https://github.com/LavX/bazarr-provider-catalog
#   docs/writing-a-scraper-provider.md in that repo explains how to port one.
#
# A pull request against this file will most likely be asked to move.

from __future__ import absolute_import

from subliminal_patch.providers.avistaz_network import AvistazNetworkProviderBase


class CinemazProvider(AvistazNetworkProviderBase):
    """CinemaZ.to Provider."""
    server_url = 'https://cinemaz.to/'
    provider_name = 'cinemaz'
