# coding=utf-8

from flask_restx import Namespace

api_ns_silo = Namespace('Silo', description='Native Silo server management')

from .endpoints import *  # noqa: E402, F403

api_ns_list_silo = [api_ns_silo]
