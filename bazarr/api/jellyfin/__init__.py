# coding=utf-8

from flask_restx import Namespace
api_ns_jellyfin = Namespace('Jellyfin', description='Jellyfin server management')

from .endpoints import *  # noqa: E402, F403
api_ns_list_jellyfin = [api_ns_jellyfin]
