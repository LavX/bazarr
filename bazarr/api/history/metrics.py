# coding=utf-8

import datetime
import operator

from flask_restx import Resource, Namespace, reqparse, fields, marshal
from functools import reduce
from sqlalchemy import case, func, or_

from app.database import (TableBlacklist, TableBlacklistMovie, TableBlacklistSports, TableHistory,
                          TableHistoryMovie, TableHistorySports, database, select)
from subliminal_patch.score import MAX_SCORES

from ..utils import authenticate

api_ns_history_metrics = Namespace('History Metrics', description='Aggregate subtitle download metrics')

# Seconds per supported timeframe. Validated up front so an unknown value is a
# 400 rather than an unbound local and a 500.
TIMEFRAMES = {
    'week': 6 * 24 * 60 * 60,
    'month': 30 * 24 * 60 * 60,
    'trimester': 90 * 24 * 60 * 60,
    'year': 364 * 24 * 60 * 60,
}

# Acquisition actions. The others are deliberately excluded from the default:
# 0 deletes nothing was acquired, 4 uploads and 7 embedded scans are written
# with a synthetic perfect score, and 6 translations carry a score derived from
# a settings field. Averaging any of them measures configuration, not quality.
ACQUISITION_ACTIONS = [1, 2, 3]

# score_out_of is only written when score is truthy, so it is NULL for a
# legitimate zero score. Each table's denominator is a known constant, so it
# can be filled in rather than dropping the row and biasing quality upward.
EPISODE_MAX = MAX_SCORES['episode']
MOVIE_MAX = MAX_SCORES['movie']

# Buckets of ten percentage points, plus an overflow bucket. A hash match
# scores 359 on top of other matches against a 360 denominator, so above 100%
# is legitimate and belongs in its own bucket rather than clipped into 90-100.
SCORE_BUCKETS = 10


def _percent(table, denominator):
    """Match score as a percentage of the achievable score.

    The 1.0 multiplier matters: both SQLite and PostgreSQL do integer division
    on int/int, which would truncate every bucket and read uniformly low.
    """
    return 1.0 * table.score * 100 / func.coalesce(table.score_out_of, denominator)


def _bucket_expression(percent):
    """Ten-point bucket for a score percentage, as an explicit threshold CASE.

    Deliberately not ``CAST(percent / 10 AS INTEGER)``: that rounds on
    PostgreSQL and truncates on SQLite, so 28.9% lands in bucket 3 on one
    backend and bucket 2 on the other. A threshold ladder means the same
    library reports the same histogram whichever database is configured.
    """
    branches = [(percent >= 100, SCORE_BUCKETS)]
    branches.extend(
        (percent >= bucket * 10, bucket)
        for bucket in range(SCORE_BUCKETS - 1, 0, -1)
    )
    return case(*branches, else_=0)


def language_matches(column, code):
    """The selected language and its variants.

    The language selector offers base codes only, because
    /system/languages?history=true strips the suffix, while history and
    blacklist rows store ``en:hi`` or ``en:forced``. Exact equality dropped
    every variant row from the figures for its language.
    """
    return or_(column == code, column.like(f'{code}:%'))


class _Source:
    """One history table plus the filter clause selected for it."""

    def __init__(self, table, denominator, key, blacklist=None):
        self.table = table
        self.denominator = denominator
        self.key = key
        self.blacklist = blacklist
        self.clauses = []

    @property
    def where(self):
        return reduce(operator.and_, self.clauses)

    @property
    def percent(self):
        return _percent(self.table, self.denominator)


@api_ns_history_metrics.route('history/metrics')
class HistoryMetrics(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('timeFrame', type=str, default='month',
                                    help='Timeframe. One of ["week", "month", "trimester", "year"]')
    get_request_parser.add_argument('action', type=str, default='All', help='Action type to filter for.')
    get_request_parser.add_argument('provider', type=str, default='All', help='Provider name to filter for.')
    get_request_parser.add_argument('language', type=str, default='All', help='Language to filter for.')

    totals_model = api_ns_history_metrics.model('HistoryMetricsTotals', {
        'downloads': fields.Integer(),
        'series': fields.Integer(),
        'movies': fields.Integer(),
        'sports': fields.Integer(),
        'dailyAverage': fields.Float(),
        'peakDate': fields.String(),
        'peakCount': fields.Integer(),
        'automaticPct': fields.Float(),
    })

    provider_model = api_ns_history_metrics.model('HistoryMetricsProvider', {
        'provider': fields.String(),
        'count': fields.Integer(),
        'avgScorePct': fields.Float(),
    })

    reliability_model = api_ns_history_metrics.model('HistoryMetricsReliability', {
        'provider': fields.String(),
        'downloads': fields.Integer(),
        'blacklisted': fields.Integer(),
        'ratePct': fields.Float(),
    })

    language_model = api_ns_history_metrics.model('HistoryMetricsLanguage', {
        'language': fields.String(),
        'count': fields.Integer(),
    })

    action_model = api_ns_history_metrics.model('HistoryMetricsAction', {
        'action': fields.Integer(),
        'count': fields.Integer(),
    })

    bucket_model = api_ns_history_metrics.model('HistoryMetricsBucket', {
        'bucket': fields.Integer(),
        'count': fields.Integer(),
    })

    get_response_model = api_ns_history_metrics.model('HistoryMetricsGetResponse', {
        'totals': fields.Nested(totals_model),
        'byProvider': fields.List(fields.Nested(provider_model)),
        'providerReliability': fields.List(fields.Nested(reliability_model)),
        'byLanguage': fields.List(fields.Nested(language_model)),
        'byAction': fields.List(fields.Nested(action_model)),
        'scoreHistogram': fields.List(fields.Nested(bucket_model)),
    })

    @authenticate
    @api_ns_history_metrics.response(400, 'Invalid filter')
    @api_ns_history_metrics.response(401, 'Not Authenticated')
    @api_ns_history_metrics.doc(parser=get_request_parser)
    def get(self):
        """Aggregate download metrics over a timeframe"""
        args = self.get_request_parser.parse_args()
        timeframe = args.get('timeFrame')
        action = args.get('action')
        provider = args.get('provider')
        language = args.get('language')

        if timeframe not in TIMEFRAMES:
            return f'timeFrame must be one of {sorted(TIMEFRAMES)}', 400

        if action != 'All':
            try:
                action = int(action)
            except (TypeError, ValueError):
                return 'action must be a number or "All"', 400

        delay = TIMEFRAMES[timeframe]
        now = datetime.datetime.now()
        past = now - datetime.timedelta(seconds=delay)
        # Inclusive day count, matching the number of daily buckets the
        # Activity chart draws for the same timeframe (a 6-day 'week' delay
        # spans 7 calendar days). Dividing by the delay alone would overstate
        # the average against the chart the user is looking at.
        days = max(delay // (24 * 60 * 60) + 1, 1)

        sources = [
            _Source(TableHistory, EPISODE_MAX, 'series', TableBlacklist),
            _Source(TableHistoryMovie, MOVIE_MAX, 'movies', TableBlacklistMovie),
            # Sports history stores MAX_SCORES['movie'] as its denominator.
            _Source(TableHistorySports, MOVIE_MAX, 'sports', TableBlacklistSports),
        ]

        for source in sources:
            table = source.table
            source.clauses.append(table.timestamp.between(past, now))
            if action != 'All':
                source.clauses.append(table.action == action)
            else:
                source.clauses.append(table.action.in_(ACQUISITION_ACTIONS))
            if provider != 'All':
                source.clauses.append(table.provider == provider)
            if language != 'All':
                source.clauses.append(language_matches(table.language, language))

        totals = {'downloads': 0, 'series': 0, 'movies': 0, 'sports': 0}
        per_provider = {}
        per_language = {}
        per_action = {}
        per_day = {}
        histogram = {bucket: 0 for bucket in range(SCORE_BUCKETS + 1)}

        for source in sources:
            table = source.table

            count = database.execute(
                select(func.count()).select_from(table).where(source.where)).scalar() or 0
            totals[source.key] = count
            totals['downloads'] += count

            # Provider leaderboard. The percentage is normalised per table, so
            # a perfect episode (360/360) and a perfect movie (180/180) both
            # read as 100% instead of averaging to a meaningless 270. AVG skips
            # a NULL score, so each table's average is weighted by its scored
            # rows, not by every row it counted.
            for row in database.execute(
                    select(table.provider,
                           func.count().label('count'),
                           func.count(source.percent).label('scored'),
                           func.avg(source.percent).label('avg_pct'))
                    .where(source.where)
                    .group_by(table.provider)).all():
                bucket = per_provider.setdefault(row.provider, {'count': 0, 'scored': 0, 'weighted': 0.0})
                bucket['count'] += row.count
                if row.scored:
                    bucket['scored'] += row.scored
                    bucket['weighted'] += float(row.avg_pct) * row.scored

            for row in database.execute(
                    select(table.language, func.count().label('count'))
                    .where(source.where)
                    .group_by(table.language)).all():
                per_language[row.language] = per_language.get(row.language, 0) + row.count

            for row in database.execute(
                    select(table.action, func.count().label('count'))
                    .where(source.where)
                    .group_by(table.action)).all():
                per_action[row.action] = per_action.get(row.action, 0) + row.count

            # func.date returns TEXT on SQLite and a date object on PostgreSQL,
            # so normalise to a string before it reaches the response.
            for row in database.execute(
                    select(func.date(table.timestamp).label('day'), func.count().label('count'))
                    .where(source.where)
                    .group_by(func.date(table.timestamp))).all():
                day = row.day if isinstance(row.day, str) else row.day.strftime('%Y-%m-%d')
                per_day[day] = per_day.get(day, 0) + row.count

            # A NULL score is unmeasured, not a 0% match: every comparison in
            # the CASE is unknown, so it would fall through to bucket 0.
            bucket_expr = _bucket_expression(source.percent)
            for row in database.execute(
                    select(bucket_expr.label('bucket'), func.count().label('count'))
                    .where(source.where, table.score.is_not(None))
                    .group_by(bucket_expr)).all():
                if row.bucket is None:
                    continue
                index = max(0, min(SCORE_BUCKETS, int(row.bucket)))
                histogram[index] += row.count

        # Blacklist rate per provider: the numerator is every blacklisted
        # subtitle from that provider in the window, the denominator is every
        # download from it. Matching pairs row by row is not the question.
        #
        # An exclusion does not record which kind of download it undid, so
        # with one action selected the numerator would still hold every
        # action's exclusions. The rate is reported as unavailable then.
        reliability_available = action == 'All'
        blacklisted = {}
        if reliability_available:
            for source in sources:
                table = source.blacklist
                clauses = [table.timestamp.between(past, now)]
                if language != 'All':
                    clauses.append(language_matches(table.language, language))
                for row in database.execute(
                        select(table.provider, func.count().label('count'))
                        .where(*clauses)
                        .group_by(table.provider)).all():
                    if row.provider is None:
                        continue
                    blacklisted[row.provider] = blacklisted.get(row.provider, 0) + row.count

        by_provider = []
        reliability = [] if reliability_available else None
        for name, stats in per_provider.items():
            if name is None:
                continue
            count = stats['count']
            scored = stats['scored']
            by_provider.append({
                'provider': name,
                'count': count,
                'avgScorePct': round(stats['weighted'] / scored, 1) if scored else None,
            })
            if reliability is None:
                continue
            blocked = blacklisted.get(name, 0)
            reliability.append({
                'provider': name,
                'downloads': count,
                'blacklisted': blocked,
                'ratePct': round(1.0 * blocked * 100 / count, 1) if count else 0.0,
            })

        by_provider.sort(key=lambda row: (-row['count'], row['provider']))
        if reliability is not None:
            reliability.sort(key=lambda row: (-row['ratePct'], row['provider']))

        peak_date, peak_count = (None, 0)
        if per_day:
            peak_date, peak_count = max(per_day.items(), key=lambda kv: (kv[1], kv[0]))

        automatic = per_action.get(1, 0) + per_action.get(3, 0)
        totals['dailyAverage'] = round(totals['downloads'] / days, 2)
        totals['peakDate'] = peak_date
        totals['peakCount'] = peak_count
        totals['automaticPct'] = (
            round(1.0 * automatic * 100 / totals['downloads'], 1) if totals['downloads'] else 0.0
        )

        return marshal({
            'totals': totals,
            'byProvider': by_provider,
            'providerReliability': reliability,
            'byLanguage': [{'language': k, 'count': v}
                           for k, v in sorted(per_language.items(), key=lambda kv: (-kv[1], kv[0] or ''))
                           if k is not None],
            'byAction': [{'action': k, 'count': v}
                         for k, v in sorted(per_action.items()) if k is not None],
            'scoreHistogram': [{'bucket': k, 'count': v} for k, v in sorted(histogram.items())],
        }, self.get_response_model)
