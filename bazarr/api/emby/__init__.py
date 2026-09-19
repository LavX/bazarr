# coding=utf-8

from flask_restx import Namespace

api_ns_emby = Namespace('Emby', description='Emby server management')

from .endpoints import *  # noqa: E402, F403

api_ns_list_emby = [api_ns_emby]
