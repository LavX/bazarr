# coding=utf-8

import os
import platform
import logging
import sys

from flask_restx import Resource, Namespace
from tzlocal import get_localzone_name
from alembic.migration import MigrationContext

from radarr.info import get_radarr_info
from sonarr.info import get_sonarr_info
from app.get_args import args
from app.database import engine, database, select
from init import startTime

from ..utils import authenticate

api_ns_system_status = Namespace('System Status', description='List environment information and versions')


@api_ns_system_status.route('system/status')
class SystemStatus(Resource):
    @authenticate
    @api_ns_system_status.response(200, "Success")
    @api_ns_system_status.response(401, 'Not Authenticated')
    def get(self):
        """Return environment information and versions"""
        package_version = ''
        if 'BAZARR_PACKAGE_VERSION' in os.environ:
            package_version = os.environ['BAZARR_PACKAGE_VERSION']
        if 'BAZARR_PACKAGE_AUTHOR' in os.environ and os.environ['BAZARR_PACKAGE_AUTHOR'] != '':
            package_version = f'{package_version} by {os.environ["BAZARR_PACKAGE_AUTHOR"]}'

        try:
            timezone = get_localzone_name() or "Undefined"
        except Exception:
            timezone = "Exception while getting time zone name."
            logging.exception("BAZARR is unable to get configured time zone name.")

        try:
            database_version = ".".join([str(x) for x in engine.dialect.server_version_info])
        except Exception:
            database_version = ""

        try:
            database_migration = MigrationContext.configure(engine.connect()).get_current_revision()
        except Exception:
            database_migration = "unknown"

        system_status = {}
        system_status.update({'bazarr_version': os.environ["BAZARR_VERSION"]})
        system_status.update({'package_version': package_version})
        system_status.update({'sonarr_version': get_sonarr_info.version()})
        system_status.update({'radarr_version': get_radarr_info.version()})
        system_status.update({'operating_system': platform.platform()})
        
        # Check if JIT is enabled (Python 3.13+)
        python_version = platform.python_version()
        jit_status = None
        try:
            # Python 3.13+ has sys._jit module when JIT is available
            if hasattr(sys, '_jit') and hasattr(sys._jit, 'is_enabled'):
                jit_enabled = sys._jit.is_enabled()
                jit_status = "JIT enabled" if jit_enabled else "JIT disabled"
            elif os.environ.get('PYTHON_JIT', '0') == '1':
                # Fallback: check environment variable
                jit_status = "JIT requested (env)"
        except Exception:
            pass
        
        if jit_status:
            python_version = f"{python_version} ({jit_status})"
        
        system_status.update({'python_version': python_version})
        system_status.update({'database_engine': f'{engine.dialect.name.capitalize()} {database_version}'})
        system_status.update({'database_migration': database_migration})
        system_status.update({'bazarr_directory': os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(__file__))))})
        system_status.update({'bazarr_config_directory': args.config_dir})
        system_status.update({'start_time': startTime})
        system_status.update({'timezone': timezone})
        system_status.update({'cpu_cores': os.cpu_count()})

        return {'data': system_status}
