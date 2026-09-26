# coding=utf-8

from flask_restx import Namespace
api_ns_seerr = Namespace('Seerr', description='Seerr, Jellyseerr and Overseerr requests')

from .endpoints import *  # noqa: E402, F403
api_ns_list_seerr = [api_ns_seerr]
