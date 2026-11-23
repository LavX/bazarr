# coding=utf-8

from flask_restx import Resource, Namespace, inputs, reqparse, fields, marshal

from arr_instances.resolution import scoped
from app.database import TableMovies, TableHistoryMovie, database, update, select, func
from radarr.sync.movies import sync_one_movie
from subtitles.indexer.missing_refresh import queue_missing_subtitles_recalculation
from subtitles.indexer.movies import movies_scan_disk
from app.event_handler import event_stream
from subtitles.wanted import wanted_search_missing_subtitles_movies, wanted_scan_subtitles_movies
from subtitles.mass_download import movies_download_subtitles
from api.queued_job import queued_job_model, queued_job_response
from api.swaggerui import subtitles_model, subtitles_language_model, audio_language_model, job_queued_model

from api.utils import authenticate, None_Keys, lowest_subtitle_scores
from media_sources import repository
from media_sources.library_rows import library_page, library_row, library_row_fields, movie_rows
from media_sources.union import SourceKeyFilter, add_source_arguments, includes_sources

api_ns_movies = Namespace('Movies', description='List movies metadata, update movie languages profile or run actions '
                                                'for specific movies.')


@api_ns_movies.route('movies')
class Movies(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('start', type=int, required=False, default=0, help='Paging start integer')
    get_request_parser.add_argument('length', type=int, required=False, default=-1, help='Paging length integer')
    get_request_parser.add_argument('radarrid[]', type=int, action='append', required=False, default=[],
                                    help='Upstream Radarr movie IDs (legacy; not unique across instances)')
    get_request_parser.add_argument('id[]', type=int, action='append', required=False, default=[],
                                    help='Canonical local movie IDs (#156; preferred, unique across instances)')
    get_request_parser.add_argument('scores', type=inputs.boolean, required=False, default=False,
                                    help='Add lowest_subtitle_score per movie (opt-in; absent = unchanged response)')

    add_source_arguments(get_request_parser)
    get_request_parser.add_argument('needs_attention', type=inputs.boolean, location='args', default=False,
                                    help='Only source rows with unresolved identity or no effective profile')

    get_subtitles_model = api_ns_movies.model('subtitles_model', subtitles_model)
    get_subtitles_language_model = api_ns_movies.model('subtitles_language_model', subtitles_language_model)
    get_audio_language_model = api_ns_movies.model('audio_language_model', audio_language_model)

    data_model = api_ns_movies.model('movies_data_model', {
        # Canonical local id + owning instance (#156); additive, frontend
        # migrates to id while radarrId stays for back-compat.
        'id': fields.Integer(),
        'arr_instance_id': fields.Integer(),
        'alternativeTitles': fields.List(fields.String),
        'audio_language': fields.Nested(get_audio_language_model),
        'fanart': fields.String(),
        'imdbId': fields.String(),
        'missing_subtitles': fields.Nested(get_subtitles_language_model),
        'monitored': fields.Boolean(),
        'overview': fields.String(),
        'path': fields.String(),
        'poster': fields.String(),
        'profileId': fields.Integer(),
        'radarrId': fields.Integer(),
        'sceneName': fields.String(),
        'subtitles': fields.Nested(get_subtitles_model),
        'tags': fields.List(fields.String),
        'title': fields.String(),
        'year': fields.String(),
    })

    get_response_model = api_ns_movies.model('MoviesGetResponse', {
        'data': fields.Nested(data_model),
        'total': fields.Integer(),
    })

    # Opt-in variant (scores=1): adds lowest_subtitle_score. Kept as a separate
    # model so the default response stays byte-identical (marshal drops fields
    # the model does not declare).
    data_model_with_scores = api_ns_movies.clone('movies_data_model_scores', data_model, {
        'lowest_subtitle_score': fields.Float(),
    })

    get_response_model_with_scores = api_ns_movies.model('MoviesGetResponseScores', {
        'data': fields.Nested(data_model_with_scores),
        'total': fields.Integer(),
    })

    mixed_data_model = api_ns_movies.clone('movies_mixed_data', data_model, library_row_fields)
    mixed_data_model_with_scores = api_ns_movies.clone(
        'movies_mixed_data_scores', data_model_with_scores, library_row_fields)

    @authenticate
    @api_ns_movies.doc(parser=get_request_parser)
    @api_ns_movies.response(200, 'Success')
    @api_ns_movies.response(401, 'Not Authenticated')
    def get(self):
        """List movies metadata for specific movies"""
        args = self.get_request_parser.parse_args()
        start = args.get('start')
        length = args.get('length')
        radarrId = args.get('radarrid[]')
        localId = args.get('id[]')
        scores = args.get('scores')
        needs_attention = args.get('needs_attention')
        owner_filter = SourceKeyFilter(tuple(args.get('source_key[]') or ()))
        explicit_ids = bool(localId or radarrId)
        source_table = repository.Items
        mixed = (not explicit_ids and args.get('sources') == '1' and includes_sources(
            args, database, source_table, source_table.c.kind == 'movie'))
        if needs_attention and not mixed:
            return {'data': [], 'total': 0}

        stmt = select(TableMovies.id,
                      TableMovies.arr_instance_id,
                      TableMovies.alternativeTitles,
                      TableMovies.audio_language,
                      TableMovies.fanart,
                      TableMovies.imdbId,
                      TableMovies.missing_subtitles,
                      TableMovies.monitored,
                      TableMovies.overview,
                      TableMovies.path,
                      TableMovies.poster,
                      TableMovies.profileId,
                      TableMovies.radarrId,
                      TableMovies.sceneName,
                      TableMovies.subtitles,
                      TableMovies.tags,
                      TableMovies.title,
                      TableMovies.year,
                      )\
            .order_by(TableMovies.sortTitle)

        # Prefer the canonical local id (#156); fall back to the upstream id for
        # back-compat (old bookmarks, the not-yet-migrated action layer).
        if len(localId) != 0:
            stmt = stmt.where(TableMovies.id.in_(localId))
        elif len(radarrId) != 0:
            stmt = stmt.where(TableMovies.radarrId.in_(radarrId))

        if owner_filter.keys:
            stmt = stmt.where(owner_filter.arr(TableMovies.arr_instance_id))
        if mixed:
            rows, count = library_page(database, stmt, movie_rows(owner_filter), TableMovies.sortTitle,
                                       start=start, length=length, needs_attention=needs_attention)
        else:
            count_query = select(func.count()).select_from(TableMovies)
            if owner_filter.keys:
                count_query = select(func.count()).select_from(stmt.order_by(None).subquery())
            if length > 0:
                stmt = stmt.limit(length).offset(start)
            rows = database.execute(stmt).all()
            count = database.execute(count_query).scalar()
        results = [library_row(row, mixed) for row in rows]

        if scores:
            arr_rows = [row for row in rows if not mixed or row.origin == 'arr']
            score_map = lowest_subtitle_scores(database, {x.id: x.subtitles for x in arr_rows},
                                               TableHistoryMovie.movie_id, TableHistoryMovie,
                                               videos_by_id={x.id: (x.path, x.arr_instance_id) for x in arr_rows},
                                               media_type='movie') if arr_rows else {}
            for item in results:
                item['lowest_subtitle_score'] = score_map.get(item['id']) if item.get('origin') != 'source' else None

        if mixed:
            model = self.mixed_data_model_with_scores if scores else self.mixed_data_model
            response_model = {**self.get_response_model, 'data': fields.Nested(model)}
        else:
            response_model = self.get_response_model_with_scores if scores else self.get_response_model
        return marshal({'data': results, 'total': count}, response_model)

    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('radarrid', type=int, action='append', required=False, default=[],
                                     help='Radarr movie(s) ID')
    post_request_parser.add_argument('id', type=int, action='append', required=False, default=[],
                                     help='Canonical local movie ID(s) (#156; preferred)')
    post_request_parser.add_argument('arr_instance_id', type=int, action='append', required=False, default=[],
                                     help='Owning Radarr instance id for legacy radarrid fallback (#156)')
    post_request_parser.add_argument('profileid', type=str, action='append', required=False, default=[],
                                     help='Languages profile(s) ID or "none"')

    @authenticate
    @api_ns_movies.doc(parser=post_request_parser)
    @api_ns_movies.response(204, 'Success')
    @api_ns_movies.response(401, 'Not Authenticated')
    @api_ns_movies.response(404, 'Languages profile not found')
    def post(self):
        """Update specific movies languages profile"""
        args = self.post_request_parser.parse_args()
        radarrIdList = args.get('radarrid')
        localIdList = args.get('id')
        arrInstanceIdList = args.get('arr_instance_id')
        profileIdList = args.get('profileid')
        targetList = localIdList if localIdList else radarrIdList
        changed = []

        for idx in range(len(targetList)):
            profileId = profileIdList[idx]

            if profileId in None_Keys:
                profileId = None
            else:
                try:
                    profileId = int(profileId)
                except Exception:
                    return 'Languages profile not found', 404

            if localIdList:
                localId = targetList[idx]
                movie = database.execute(
                    select(TableMovies.radarrId, TableMovies.arr_instance_id)
                    .where(TableMovies.id == localId))\
                    .first()
                if not movie:
                    continue
                database.execute(
                    update(TableMovies)
                    .values(profileId=profileId)
                    .where(TableMovies.id == localId))
                radarrId = movie.radarrId
                arr_instance_id = movie.arr_instance_id
            else:
                radarrId = targetList[idx]
                arr_instance_id = arrInstanceIdList[idx] if idx < len(arrInstanceIdList) else None
                if arr_instance_id is None:
                    matches = database.execute(
                        select(TableMovies.id).where(TableMovies.radarrId == radarrId)
                    ).all()
                    if len(matches) > 1:
                        return 'Ambiguous Radarr movie ID; pass id or arr_instance_id', 400
                else:
                    movie = database.execute(
                        scoped(
                            select(TableMovies.id).where(TableMovies.radarrId == radarrId),
                            TableMovies.arr_instance_id,
                            arr_instance_id,
                        )
                    ).first()
                    if not movie:
                        continue
                database.execute(
                    scoped(
                        update(TableMovies)
                        .values(profileId=profileId)
                        .where(TableMovies.radarrId == radarrId),
                        TableMovies.arr_instance_id,
                        arr_instance_id,
                    ))

            changed.append((radarrId, arr_instance_id))
            event_stream(type='movie', payload=radarrId)

        # Recalculated by a queued job, which announces the wanted rows and the
        # badges once it has, so the save no longer waits for it.
        queue_missing_subtitles_recalculation(movies=changed)

        return '', 204

    patch_request_parser = reqparse.RequestParser()
    patch_request_parser.add_argument('radarrid', type=int, required=False, help='Radarr movie ID')
    patch_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                      help='Owning Radarr instance id (#156)')
    patch_request_parser.add_argument('sources', type=int, required=False,
                                      help='Set to 1 to include media sources in Search All')
    patch_request_parser.add_argument('action', type=str, required=False, help='Action to perform from ["scan-disk", '
                                                                               '"search-missing", "search-wanted", "sync"]')

    patch_job_model = api_ns_movies.model('JobQueuedInSession', {**job_queued_model, 'session': queued_job_model['session']})

    @authenticate
    @api_ns_movies.doc(parser=patch_request_parser)
    @api_ns_movies.response(202, 'scan-disk and sync queued as a job', patch_job_model)
    @api_ns_movies.response(204, 'Success for every other action')
    @api_ns_movies.response(400, 'Unknown action')
    @api_ns_movies.response(401, 'Not Authenticated')
    @api_ns_movies.response(500, 'Movie file not found. Path mapping issue?')
    def patch(self):
        """Run actions on specific movies"""
        args = self.patch_request_parser.parse_args()
        radarrid = args.get('radarrid')
        arr_instance_id = args.get('arr_instance_id')
        action = args.get('action')
        if action == "scan-disk":
            job_id = movies_scan_disk(radarrid, arr_instance_id=arr_instance_id)
            return queued_job_response(job_id or None), 202
        elif action == "search-missing":
            try:
                movies_download_subtitles(radarrid, arr_instance_id=arr_instance_id)
            except OSError:
                return 'Movie file not found. Path mapping issue?', 500
            else:
                return '', 204
        elif action == "search-wanted":
            from media_sources.wanted import arr_wanted_condition, queue_wanted_search

            source_job = queue_wanted_search('movie') if args.get("sources") == 1 else None
            if not source_job:
                wanted_search_missing_subtitles_movies()
            elif database.scalar(select(select(TableMovies.id).where(
                    arr_wanted_condition('radarr', TableMovies.arr_instance_id)).exists())):
                wanted_search_missing_subtitles_movies(active_owners_only=True)
            return '', 204
        elif action == "scan-wanted":
            wanted_scan_subtitles_movies()
            return '', 204
        elif action == "sync":
            return queued_job_response(sync_one_movie(radarrid, arr_instance_id=arr_instance_id) or None), 202

        return 'Unknown action', 400
