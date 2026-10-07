# coding=utf-8

from apscheduler.jobstores.base import JobLookupError
from flask_restx import Resource, Namespace, reqparse, fields, marshal

from app import activity
from app.scheduler import scheduler

from ..utils import authenticate

api_ns_system_tasks = Namespace('System Tasks', description='List or execute tasks')


@api_ns_system_tasks.route('system/tasks')
class SystemTasks(Resource):
    get_response_model = api_ns_system_tasks.model('SystemBackupsGetResponse', {
        'interval': fields.String(),
        'job_id': fields.String(),
        'job_running': fields.Boolean(),
        'name': fields.String(),
        'next_run_in': fields.String(),
        'next_run_time': fields.String(),
    })

    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('taskid', type=str, required=False, help='List tasks or a single task properties')

    @authenticate
    @api_ns_system_tasks.doc(parser=None)
    @api_ns_system_tasks.response(200, 'Success')
    @api_ns_system_tasks.response(401, 'Not Authenticated')
    def get(self):
        """List tasks"""
        args = self.get_request_parser.parse_args()
        taskid = args.get('taskid')

        task_list = scheduler.get_task_list()

        if taskid:
            for item in task_list:
                if item['job_id'] == taskid:
                    task_list = [item]
                    continue

        return marshal(task_list, self.get_response_model, envelope='data')

    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('taskid', type=str, required=True, help='Task id of the task to run')

    @authenticate
    @api_ns_system_tasks.doc(parser=post_request_parser)
    @api_ns_system_tasks.response(204, 'Success')
    @api_ns_system_tasks.response(401, 'Not Authenticated')
    def post(self):
        """Run task"""
        args = self.post_request_parser.parse_args()
        taskid = args.get('taskid')

        # Run Now is the user's own press, so the mark tells the next execution
        # of the task through the scheduler's pool that its jobs announce. A
        # task id the scheduler never registered fails the press, so the mark
        # goes with it instead of waiting for a routine run to consume.
        activity.request_user_run(taskid)
        try:
            scheduler.execute_job_now(taskid)
        except JobLookupError:
            activity.drop_user_run(taskid)
            raise

        return '', 204
