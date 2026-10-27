# coding=utf-8

from flask_restx import Resource, Namespace

from utilities.health import check_health, get_health_issues

from ..queued_job import queued_job_model, queued_job_response
from ..utils import authenticate

api_ns_system_health = Namespace('System Health', description='List health issues')


@api_ns_system_health.route('system/health')
class SystemHealth(Resource):
    @authenticate
    @api_ns_system_health.doc(parser=None)
    @api_ns_system_health.response(200, 'Success')
    @api_ns_system_health.response(401, 'Not Authenticated')
    def get(self):
        """List health issues"""
        return {'data': get_health_issues()}

    post_job_model = api_ns_system_health.model('HealthCheckQueued', queued_job_model)

    @authenticate
    @api_ns_system_health.doc(parser=None)
    @api_ns_system_health.response(202, 'Re-check queued as a job', post_job_model)
    @api_ns_system_health.response(401, 'Not Authenticated')
    def post(self):
        """Re-check health issues

        Answers at once with the job doing it, which the request used to wait for. Read the list again when
        the job has finished."""
        return queued_job_response(check_health()), 202
