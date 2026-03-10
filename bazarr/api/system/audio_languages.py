# coding=utf-8

import ast
import logging

from flask_restx import Resource, Namespace
from operator import itemgetter

from app.database import TableMovies, TableEpisodes, database, select
from languages.get_languages import alpha2_from_language, language_from_alpha2

from ..utils import authenticate

api_ns_system_audio_languages = Namespace('System Audio Languages',
                                          description='Get unique audio languages from media library')


@api_ns_system_audio_languages.route('system/languages/audio')
class AudioLanguages(Resource):
    @authenticate
    @api_ns_system_audio_languages.response(200, 'Success')
    @api_ns_system_audio_languages.response(401, 'Not Authenticated')
    def get(self):
        """List unique audio languages found in movies and episodes"""
        lang_set = set()

        # Collect from movies
        movie_rows = database.execute(
            select(TableMovies.audio_language)
            .where(TableMovies.audio_language.is_not(None))
            .where(TableMovies.audio_language != '[]')
        ).all()

        for row in movie_rows:
            try:
                langs = ast.literal_eval(row.audio_language or '[]')
                for lang in langs:
                    if lang:
                        lang_set.add(lang)
            except (ValueError, SyntaxError):
                continue

        # Collect from episodes
        episode_rows = database.execute(
            select(TableEpisodes.audio_language)
            .where(TableEpisodes.audio_language.is_not(None))
            .where(TableEpisodes.audio_language != '[]')
        ).all()

        for row in episode_rows:
            try:
                langs = ast.literal_eval(row.audio_language or '[]')
                for lang in langs:
                    if lang:
                        lang_set.add(lang)
            except (ValueError, SyntaxError):
                continue

        # Convert to code2/name format
        result = []
        seen_codes = set()
        for lang_name in lang_set:
            try:
                code2 = alpha2_from_language(lang_name)
                if code2 and code2 not in seen_codes:
                    seen_codes.add(code2)
                    name = language_from_alpha2(code2)
                    result.append({'code2': code2, 'name': name or lang_name})
            except Exception:
                logging.debug(f"Could not resolve audio language: {lang_name}")
                continue

        return sorted(result, key=itemgetter('name'))
