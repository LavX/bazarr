# coding=utf-8
"""What a route that queues a job answers with.

What the job produced, such as the rows of a search, is not in this answer. It is the job result, read
from ``system/jobs/result`` once the job has finished.
"""

from flask_restx import fields

from app.process_session import PROCESS_SESSION

queued_job_model = {
    "job_id": fields.Integer(description="Id of the queued job, or of the identical job already running that "
                                         "this request follows."),
    "session": fields.String(description="The backend run that owns the job. Job ids restart with the backend, "
                                         "so a job is named by its id together with this."),
}


def queued_job_response(job_id):
    """The body of a 202 for a queued job."""
    return {"job_id": job_id, "session": PROCESS_SESSION}
