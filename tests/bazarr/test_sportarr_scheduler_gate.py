# coding=utf-8
"""The Use Sportarr master toggle, and the one-time reconcile that sets it.

Every Sports surface is gated on general.use_sportarr. The flag defaults to
False, while the rule it replaces was "any enabled Sportarr instance exists", so
an install upgrading into the flag would boot with sports silently gone. The
reconcile closes that gap exactly once, and the marker is what keeps it from
re-enabling a flag the operator deliberately turned off.
"""


class FakeScheduler:
    """Minimal APScheduler stand-in: only the four methods the job wiring uses."""

    def __init__(self):
        self.jobs = {}

    def add_job(self, *args, **kwargs):
        self.jobs[kwargs["id"]] = kwargs

    def get_jobs(self):
        return [type("J", (), {"id": job_id})() for job_id in list(self.jobs)]

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def remove_job(self, job_id):
        self.jobs.pop(job_id, None)


def test_no_jobs_are_registered_while_the_toggle_is_off(schema_session, monkeypatch):
    from app.config import settings
    from arr_instances.repository import ArrInstanceRepository
    from sportarr.scheduler import configure_sports_jobs

    ArrInstanceRepository(schema_session).create("sportarr", "Main", api_key="k")
    monkeypatch.setattr(settings.general, "use_sportarr", False)

    scheduler = FakeScheduler()
    configure_sports_jobs(scheduler, schema_session)
    assert scheduler.jobs == {}


def test_turning_the_toggle_off_removes_jobs_already_registered(schema_session, monkeypatch):
    from app.config import settings
    from arr_instances.repository import ArrInstanceRepository
    from sportarr.scheduler import configure_sports_jobs

    instance = ArrInstanceRepository(schema_session).create("sportarr", "Main", api_key="k")
    scheduler = FakeScheduler()

    monkeypatch.setattr(settings.general, "use_sportarr", True)
    configure_sports_jobs(scheduler, schema_session)
    assert f"update_sports_{instance.id}" in scheduler.jobs

    monkeypatch.setattr(settings.general, "use_sportarr", False)
    configure_sports_jobs(scheduler, schema_session)
    assert scheduler.jobs == {}


def test_reconcile_enables_the_flag_once_for_an_existing_install(schema_session, monkeypatch):
    from app import config
    from app.config import settings
    from arr_instances.repository import ArrInstanceRepository
    from arr_instances.service import reconcile_sportarr_enable_flag

    monkeypatch.setattr(config, "write_config", lambda: None)
    monkeypatch.setattr(settings.general, "use_sportarr", False)
    monkeypatch.setattr(settings.sportarr, "enable_reconciled", False)
    ArrInstanceRepository(schema_session).create("sportarr", "Main", api_key="k")

    assert reconcile_sportarr_enable_flag(schema_session) is True
    assert settings.general.use_sportarr is True
    assert settings.sportarr.enable_reconciled is True

    # Second boot: the operator turns it off. The marker keeps it off.
    settings.general.use_sportarr = False
    assert reconcile_sportarr_enable_flag(schema_session) is False
    assert settings.general.use_sportarr is False


def test_reconcile_leaves_the_flag_off_when_no_instance_exists(schema_session, monkeypatch):
    from app import config
    from app.config import settings
    from arr_instances.service import reconcile_sportarr_enable_flag

    monkeypatch.setattr(config, "write_config", lambda: None)
    monkeypatch.setattr(settings.general, "use_sportarr", False)
    monkeypatch.setattr(settings.sportarr, "enable_reconciled", False)

    assert reconcile_sportarr_enable_flag(schema_session) is False
    assert settings.general.use_sportarr is False
    # The marker still burns, so a later-added instance does not retroactively
    # flip a flag the operator never asked for.
    assert settings.sportarr.enable_reconciled is True


def test_reconcile_ignores_a_disabled_instance(schema_session, monkeypatch):
    from app import config
    from app.config import settings
    from arr_instances.repository import ArrInstanceRepository
    from arr_instances.service import reconcile_sportarr_enable_flag

    monkeypatch.setattr(config, "write_config", lambda: None)
    monkeypatch.setattr(settings.general, "use_sportarr", False)
    monkeypatch.setattr(settings.sportarr, "enable_reconciled", False)
    repo = ArrInstanceRepository(schema_session)
    instance = repo.create("sportarr", "Main", api_key="k")
    repo.update(instance.id, enabled=False)

    assert reconcile_sportarr_enable_flag(schema_session) is False
    assert settings.general.use_sportarr is False
