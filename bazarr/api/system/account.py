# coding=utf-8

import gc
import secrets

from flask import session, request
from flask_restx import Resource, Namespace, reqparse

from app.config import settings
from utilities.helper import check_credentials, needs_password_upgrade, upgrade_password_hash

api_ns_system_account = Namespace('System Account', description='Login or logout from Bazarr UI')


@api_ns_system_account.hide
@api_ns_system_account.route('system/account')
class SystemAccount(Resource):
    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('action', type=str, required=True,
                                     help='Action from ["login", "logout", "upgrade_hash"]')
    post_request_parser.add_argument('username', type=str, required=False, help='Bazarr username')
    post_request_parser.add_argument('password', type=str, required=False, help='Bazarr password')

    @api_ns_system_account.doc(parser=post_request_parser)
    @api_ns_system_account.response(204, 'Success')
    @api_ns_system_account.response(400, 'Unknown action')
    @api_ns_system_account.response(403, 'Authentication failed')
    @api_ns_system_account.response(406, 'Browser must be closed to invalidate basic authentication')
    @api_ns_system_account.response(500, 'Unknown authentication type define in config')
    def post(self):
        """Login or logout from Bazarr UI when using form login"""
        args = self.post_request_parser.parse_args()
        if settings.auth.type not in ['form', 'basic']:
            return 'Unknown authentication type define in config', 500

        action = args.get('action')
        if action == 'login':
            username = args.get('username')
            password = args.get('password')
            if check_credentials(username, password, request):
                session['logged_in'] = True
                if needs_password_upgrade():
                    # Store password in session for upgrade (server-side only, never sent to client)
                    session['_pw_for_upgrade'] = password
                    session['_upgrade_token'] = secrets.token_urlsafe(16)
                    return {'upgrade_hash': True, 'upgrade_token': session['_upgrade_token']}, 200
                return '', 204
            else:
                session['logged_in'] = False
                return 'Authentication failed', 403
        elif action == 'logout':
            if settings.auth.type == 'basic':
                return 'Browser must be closed to invalidate basic authentication', 406
            else:
                session.clear()
                gc.collect()
                return '', 204
        elif action == 'upgrade_hash':
            # Verify upgrade token from session (no password re-transmission)
            token = args.get('password')  # reuse password field for token
            stored_token = session.get('_upgrade_token')
            stored_pw = session.get('_pw_for_upgrade')
            if not stored_token or not stored_pw or token != stored_token:
                return 'Invalid or expired upgrade token', 403
            upgrade_password_hash(stored_pw)
            session.pop('_pw_for_upgrade', None)
            session.pop('_upgrade_token', None)
            return '', 204

        return 'Unknown action', 400
