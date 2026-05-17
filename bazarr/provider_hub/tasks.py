# coding=utf-8
import logging

from .service import check_updates, refresh_catalog


def provider_hub_check_updates(wait_for_completion=True):
    try:
        refresh_catalog()
        return check_updates()
    except Exception:
        logging.exception("Provider Hub update check failed")
        raise
