# coding=utf-8

from datetime import timezone

from flask_restx import Resource, Namespace, reqparse, fields, marshal

from app.jobs_queue import jobs_queue

from ..swaggerui import job_queued_model
from ..utils import authenticate

api_ns_system_jobs = Namespace('System Jobs', description='List, force start, move or delete jobs from the queue')


class UtcTimestamp(fields.Raw):
    """An ISO 8601 timestamp in UTC with an explicit ``Z``.

    The browser reads a timestamp without an offset as its own local time, so a
    job that just finished on a server in another time zone read as hours old.
    A naive value is taken as the server's local time.
    """

    # Raw documents itself as an object. The value is always an ISO 8601 string.
    __schema_type__ = 'string'
    __schema_format__ = 'date-time'

    def format(self, value):
        return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


@api_ns_system_jobs.route('system/jobs')
class SystemJobs(Resource):
    get_response_model = api_ns_system_jobs.model('SystemJobsGetResponse', {
        'job_id': fields.Integer(),
        'job_name': fields.String(),
        'status': fields.String(),
        'last_run_time': UtcTimestamp(),
        'is_progress': fields.Boolean(),
        'is_signalr': fields.Boolean(),
        'progress_value': fields.Integer(),
        'progress_max': fields.Integer(),
        'progress_message': fields.String(),
        # Why a failed job failed, {reason, message}, and what the user can do
        # with a finished one, {kind, label, ...}. Both are set by the job and
        # are the only parts of its outcome that are sent.
        'error': fields.Raw(),
        'action': fields.Raw(),
        # Stop leaves a job completed, so this is what says it did not finish.
        'stopped': fields.Boolean(),
        'retryable': fields.Boolean(),
        'retry_of': fields.Integer(),
    })

    get_envelope_model = api_ns_system_jobs.model('SystemJobsGetEnvelope', {
        'data': fields.List(fields.Nested(get_response_model)),
    })

    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('id', type=int, required=False, help='Job ID to return', default=None)
    get_request_parser.add_argument('status', type=str, required=False, help='Job status to return', default=None,
                                    choices=['pending', 'running', 'failed', 'completed'])

    @authenticate
    @api_ns_system_jobs.doc(parser=get_request_parser)
    @api_ns_system_jobs.response(200, 'Success', get_envelope_model)
    @api_ns_system_jobs.response(401, 'Not Authenticated')
    def get(self):
        """List jobs from the queue"""
        args = self.get_request_parser.parse_args()
        job_id = args.get('id')
        status = args.get('status')
        return marshal(jobs_queue.list_jobs_from_queue(job_id=job_id, status=status), self.get_response_model,
                       envelope='data')

    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('id', type=int, required=True, help='Job ID act onto')
    post_request_parser.add_argument('action', type=str, required=True,
                                     help='Action to perform from ["force_start", "move_top", "move_bottom", "cancel", '
                                          '"retry"]')

    post_retry_model = api_ns_system_jobs.model('JobQueued', job_queued_model)

    @authenticate
    @api_ns_system_jobs.doc(parser=post_request_parser)
    @api_ns_system_jobs.response(200, 'retry queued a new job', post_retry_model)
    @api_ns_system_jobs.response(204, 'Success for every other action')
    @api_ns_system_jobs.response(400, 'Job cannot be retried')
    @api_ns_system_jobs.response(401, 'Not Authenticated')
    def post(self):
        """Force start, move to top or move to bottom of the queue a specific job"""
        args = self.post_request_parser.parse_args()
        job_id = args.get('id')
        action = args.get('action')
        if action == "force_start":
            jobs_queue.force_start_pending_job(job_id=job_id)
        elif action == "move_top":
            jobs_queue.move_job_in_pending_queue(job_id=job_id, move_destination="top")
        elif action == "move_bottom":
            jobs_queue.move_job_in_pending_queue(job_id=job_id, move_destination="bottom")
        elif action == "cancel":
            jobs_queue.cancel_running_job(job_id=job_id)
        elif action == "retry":
            new_job_id = jobs_queue.retry_job(job_id=job_id)
            if not new_job_id:
                return 'Job cannot be retried', 400
            return {'job_id': new_job_id}, 200
        return '', 204

    patch_request_parser = reqparse.RequestParser()
    patch_request_parser.add_argument('queueName', type=str, required=True, help='Jobs queue name to empty',
                                      choices=['pending', 'failed', 'completed'])

    @authenticate
    @api_ns_system_jobs.doc(parser=patch_request_parser)
    @api_ns_system_jobs.response(204, 'Success')
    @api_ns_system_jobs.response(400, 'Jobs queue name not provided')
    @api_ns_system_jobs.response(401, 'Not Authenticated')
    def patch(self):
        """Empty a specific jobs queue"""
        args = self.patch_request_parser.parse_args()
        queue_name = args.get('queueName')
        if queue_name:
            jobs_queue.empty_jobs_queue(queue_name=queue_name)
            return '', 204
        else:
            return 'Jobs queue name not provided', 400

    delete_request_parser = reqparse.RequestParser()
    delete_request_parser.add_argument('id', type=int, required=True, help='Job ID to delete from queue')

    @authenticate
    @api_ns_system_jobs.doc(parser=delete_request_parser)
    @api_ns_system_jobs.response(204, 'Success')
    @api_ns_system_jobs.response(400, 'Job ID not provided')
    @api_ns_system_jobs.response(401, 'Not Authenticated')
    def delete(self):
        """Delete a job from the queue"""
        args = self.delete_request_parser.parse_args()
        job_id = args.get('id')
        if job_id:
            deleted = jobs_queue.remove_job_from_pending_queue(job_id=job_id)
            if deleted:
                return '', 204
        return 'Job ID not provided', 400
