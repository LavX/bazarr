from datetime import datetime


def _duration_fragment(seconds, denominator=1, text="", past=True):
    result = str(round(seconds / denominator))
    if past:
        return result + text + " ago"
    return "in " + result + text


def pretty_date(time=False, asdays=False, short=False, now=None):
    now = now or datetime.now()
    if type(time) is int:
        time = datetime.fromtimestamp(time)
    elif not time:
        time = now

    if time > now:
        past, diff = False, time - now
    else:
        past, diff = True, now - time

    seconds = diff.seconds
    days = diff.days

    if short:
        if days == 0 and not asdays:
            if seconds < 10:
                return "now"
            if seconds < 60:
                return _duration_fragment(seconds, 1, "s", past)
            if seconds < 3600:
                return _duration_fragment(seconds, 60, "m", past)
            return _duration_fragment(seconds, 3600, "h", past)

        if days == 0:
            return "today"
        if days == 1:
            return "yest" if past else "tom"
        if days < 7:
            return _duration_fragment(days, 1, "d", past)
        if days < 31:
            return _duration_fragment(days, 7, "w", past)
        if days < 365:
            return _duration_fragment(days, 30, "mo", past)
        return _duration_fragment(days, 365, "y", past)

    if days == 0 and not asdays:
        if seconds < 10:
            return "now"
        if seconds < 60:
            return _duration_fragment(seconds, 1, " seconds", past)
        if seconds < 120:
            return "a minute ago" if past else "in a minute"
        if seconds < 3600:
            return _duration_fragment(seconds, 60, " minutes", past)
        if seconds < 7200:
            return "an hour ago" if past else "in an hour"
        return _duration_fragment(seconds, 3600, " hours", past)

    if days == 0:
        return "today"
    if days == 1:
        return "yesterday" if past else "tomorrow"
    if days == 2:
        return "day before" if past else "day after"
    if days < 7:
        return _duration_fragment(days, 1, " days", past)
    if days < 14:
        return "last week" if past else "next week"
    if days < 31:
        return _duration_fragment(days, 7, " weeks", past)
    if days < 61:
        return "last month" if past else "next month"
    if days < 365:
        return _duration_fragment(days, 30, " months", past)
    if days < 730:
        return "last year" if past else "next year"
    return _duration_fragment(days, 365, " years", past)
