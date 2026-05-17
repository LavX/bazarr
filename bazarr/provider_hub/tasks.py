# coding=utf-8
import logging

from .service import check_updates


def provider_hub_check_updates(wait_for_completion=True):
    try:
        return check_updates()
    except Exception:
        logging.exception("Provider Hub update check failed")
        raise
