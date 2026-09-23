# coding=utf-8


class SubtitleJobError(Exception):
    """A queued subtitle job could not do what the user asked.

    The jobs queue only marks a job failed when the job raises, so a job that
    reported its failure by returning a string or False used to end up in the
    Completed group. Raise this instead, with a sentence the user can act on:
    its text is what the job reports as the reason it failed.
    """


# How many failed item names a batch failure spells out before "and N more".
FAILURE_NAMES_SHOWN = 3


def describe_failures(failures, shown=FAILURE_NAMES_SHOWN):
    """The first few failed items of a batch, for the job's failure reason."""
    text = '; '.join(failures[:shown])
    more = len(failures) - shown
    if more > 0:
        text += f'; and {more} more'
    return text
