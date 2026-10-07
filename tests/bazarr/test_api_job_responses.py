# coding=utf-8
"""The published API reference must say when a route answers with a job.

Scan disk on a series or movie now queues a job and answers 202 with its id,
but the Swagger spec still promised a bare 204, and GET system/jobs was
documented as 204 while it returns the job list with a 200. A client built
from the spec had no way to learn that there was a job id to follow.

The spec here comes from the API's own swagger.json endpoint, the same
document the API docs page renders.
"""
import pytest
from flask import Flask


@pytest.fixture(scope="module")
def spec():
    from api import api_bp

    app = Flask(__name__)
    app.register_blueprint(api_bp)
    response = app.test_client().get("/api/swagger.json")
    assert response.status_code == 200
    return response.get_json()


def _schema_ref(response):
    return response["schema"]["$ref"].rsplit("/", 1)[-1]


def test_series_and_movie_actions_document_the_scan_job(spec):
    for path in ("/series", "/movies"):
        responses = spec["paths"][path]["patch"]["responses"]
        # scan-disk queues a job, every other action still answers 204.
        assert {"202", "204"} <= set(responses), path
        model = spec["definitions"][_schema_ref(responses["202"])]
        # An identical job already queued answers with a null id.
        assert model["properties"]["job_id"]["type"] == "integer"
        assert model["properties"]["job_id"]["x-nullable"] is True


def test_listing_jobs_documents_the_list_it_returns(spec):
    responses = spec["paths"]["/system/jobs"]["get"]["responses"]
    assert "204" not in responses
    envelope = spec["definitions"][_schema_ref(responses["200"])]
    jobs = envelope["properties"]["data"]
    assert jobs["type"] == "array"
    job = spec["definitions"][jobs["items"]["$ref"].rsplit("/", 1)[-1]]
    assert {"job_id", "status"} <= set(job["properties"])
    assert job["properties"]["last_run_time"]["type"] == "string"
    assert job["properties"]["last_run_time"]["format"] == "date-time"


def test_the_jobs_list_documents_the_fields_a_new_job_sends_as_null(spec):
    # A job that has not failed has no error, one that offers nothing has no
    # action, and only a retry has retry_of. The list sends all three as null.
    from flask_restx import marshal

    from api.system.jobs import SystemJobs
    from app.jobs_queue import Job

    sent = marshal([vars(Job(job_id=1, job_name="Example", module="m", func="f"))],
                   SystemJobs.get_response_model)[0]
    assert (sent["error"], sent["action"], sent["retry_of"]) == (None, None, None)

    responses = spec["paths"]["/system/jobs"]["get"]["responses"]
    envelope = spec["definitions"][_schema_ref(responses["200"])]
    job = spec["definitions"][envelope["properties"]["data"]["items"]["$ref"].rsplit("/", 1)[-1]]
    for name in ("error", "action", "retry_of"):
        assert job["properties"][name]["x-nullable"] is True, name
    assert job["properties"]["error"]["type"] == "object"
    assert job["properties"]["action"]["type"] == "object"
    assert job["properties"]["retry_of"]["type"] == "integer"


def test_the_jobs_list_documents_who_started_the_job(spec):
    # 'user' for a job an authenticated request enqueued, 'scheduled' for one
    # the scheduler's task pool ran, null when nothing knows.
    from flask_restx import marshal

    from api.system.jobs import SystemJobs
    from app import activity
    from app.jobs_queue import Job

    with activity.user_action():
        users = marshal([vars(Job(job_id=1, job_name="Example", module="m", func="f"))],
                        SystemJobs.get_response_model)[0]
    with activity.scheduler_run("example_task"):
        scheduled = marshal([vars(Job(job_id=2, job_name="Example", module="m", func="f"))],
                            SystemJobs.get_response_model)[0]
    bare = marshal([vars(Job(job_id=3, job_name="Example", module="m", func="f"))],
                   SystemJobs.get_response_model)[0]
    assert (users["origin"], scheduled["origin"], bare["origin"]) == ("user", "scheduled", None)

    responses = spec["paths"]["/system/jobs"]["get"]["responses"]
    envelope = spec["definitions"][_schema_ref(responses["200"])]
    job = spec["definitions"][envelope["properties"]["data"]["items"]["$ref"].rsplit("/", 1)[-1]]
    assert job["properties"]["origin"]["type"] == "string"
    assert job["properties"]["origin"]["x-nullable"] is True
