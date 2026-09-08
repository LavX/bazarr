"""Truthful provider publication and owner-safe refresh outcomes."""

from dataclasses import dataclass, field
import logging


def publication_cancelled(cancel):
    # Queue cancellation is an exception, while owner/transport cancellation is
    # a boolean. Once bytes are published, either must preserve the outcome.
    from app.jobs_queue import JobCancelled

    try:
        return cancel is not None and cancel.is_set()
    except JobCancelled:
        return True


@dataclass
class SportsSaveResult:
    result: object = None
    publication: dict = field(
        default_factory=lambda: {
            "published": False,
            "status": "not_published",
            "processing": "not_started",
            "artifact": "not_started",
            "history": "not_started",
            "index": "not_attempted",
            "refresh_attempts": 0,
            "refresh_queued": False,
            "failed_phase": None,
            "cancelled": False,
            "message": "",
        }
    )

    def refresh(self, candidate, database, cancel=None):
        from sportarr.subtitles import candidate_signature
        from sportarr.output import SportsOutputNamespace
        from subtitles.indexer.sports import store_subtitles_sports

        state = self.publication
        if publication_cancelled(cancel):
            state.update(index="cancelled", cancelled=True)
            return
        context = candidate.context
        try:
            if candidate_signature(context, database) != candidate.signature:
                raise ValueError("Sports owner or file changed")
            SportsOutputNamespace(context, database).validate(database)
        except ValueError:
            state["index"] = "owner_changed"
            return
        except Exception:
            state["index"] = "failed"
            logging.exception(
                "Published sports subtitle ownership refresh check failed"
            )
            return
        state["refresh_attempts"] += 1
        try:
            store_subtitles_sports(
                context.event_id, context.arr_instance_id, cancel=cancel
            )
            state["index"] = "completed"
        except Exception:
            state["index"] = "cancelled" if publication_cancelled(cancel) else "failed"
            state["cancelled"] = state["index"] == "cancelled"
            if not state["cancelled"]:
                try:
                    if candidate_signature(context, database) != candidate.signature:
                        state["index"] = "owner_changed"
                except ValueError:
                    state["index"] = "owner_changed"
                except Exception:
                    pass
            logging.exception("Published sports subtitle index refresh failed")

    def finish(self):
        state = self.publication
        complete = state["failed_phase"] is None and state["index"] == "completed"
        state["status"] = "published" if complete else "published_with_warnings"
        if complete:
            state["message"] = (
                "Subtitle published. Processing and history completed; subtitle index refreshed."
            )
        else:
            phase = state["failed_phase"] or "index"
            refresh = {
                "completed": "Subtitle index refreshed.",
                "cancelled": "Index refresh cancelled; no refresh queued.",
                "owner_changed": "Index refresh stopped because ownership changed; no refresh queued.",
            }.get(
                state["index"],
                "Index refresh failed; no refresh queued. Refresh this file after resolving the error.",
            )
            state["message"] = (
                f"Subtitle published; {phase} did not complete. "
                f"History {state['history'].replace('_', ' ')}. {refresh}"
            )
        return self
