# coding=utf-8

import logging
import os
import ast
import re
from app.database import TableEpisodes, TableMovies, database, select
from utilities.path_mappings import path_mappings
from subtitles.tools.translate.main import translate_subtitles_file

logger = logging.getLogger(__name__)

def process_episode_translation(item, source_language, target_language, forced, hi, subtitle_path):
    """Process a single episode for translation in background"""
    sonarr_series_id = item.get('sonarrSeriesId')
    sonarr_episode_id = item.get('sonarrEpisodeId')

    if not sonarr_series_id or not sonarr_episode_id:
        logger.error('Missing sonarrSeriesId or sonarrEpisodeId')
        return False

    # Get episode info from database
    episode = database.execute(
        select(TableEpisodes.path, TableEpisodes.subtitles, TableEpisodes.sonarrSeriesId)
        .where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id)
    ).first()

    if not episode:
        logger.error(f'Episode {sonarr_episode_id} not found')
        return False

    video_path = path_mappings.path_replace(episode.path)

    # Find source subtitle
    source_subtitle_path = subtitle_path
    detected_source_lang = None
    if not source_subtitle_path:
        source_subtitle_path, detected_source_lang = find_subtitle_by_language(
            episode.subtitles, source_language, video_path, media_type='series'
        )

    if not source_subtitle_path:
        logger.error(f'No subtitle found for episode {sonarr_episode_id} (requested source: {source_language})')
        return False

    # Use detected language if available
    if detected_source_lang:
        source_language = detected_source_lang

    # Queue translation
    try:
        result = translate_subtitles_file(
            video_path=video_path,
            source_srt_file=source_subtitle_path,
            from_lang=source_language,
            to_lang=target_language,
            forced=forced,
            hi=hi,
            media_type="series",
            sonarr_series_id=sonarr_series_id,
            sonarr_episode_id=sonarr_episode_id,
            radarr_id=None
        )
        return result is not False
    except Exception as e:
        logger.error(f'Translation failed for episode {sonarr_episode_id}: {e}')
        return False

def process_movie_translation(item, source_language, target_language, forced, hi, subtitle_path):
    """Process a single movie for translation in background"""
    radarr_id = item.get('radarrId')

    if not radarr_id:
        logger.error('Missing radarrId')
        return False

    # Get movie info from database
    movie = database.execute(
        select(TableMovies.path, TableMovies.subtitles)
        .where(TableMovies.radarrId == radarr_id)
    ).first()

    if not movie:
        logger.error(f'Movie {radarr_id} not found')
        return False

    video_path = path_mappings.path_replace_movie(movie.path)

    # Find source subtitle
    source_subtitle_path = subtitle_path
    detected_source_lang = None
    if not source_subtitle_path:
        source_subtitle_path, detected_source_lang = find_subtitle_by_language(
            movie.subtitles, source_language, video_path, media_type='movie'
        )

    if not source_subtitle_path:
        logger.error(f'No subtitle found for movie {radarr_id} (requested source: {source_language})')
        return False

    # Use detected language if available
    if detected_source_lang:
        source_language = detected_source_lang

    # Queue translation  
    try:
        result = translate_subtitles_file(
            video_path=video_path,
            source_srt_file=source_subtitle_path,
            from_lang=source_language,
            to_lang=target_language,
            forced=forced,
            hi=hi,
            media_type="movies",
            sonarr_series_id=None,
            sonarr_episode_id=None,
            radarr_id=radarr_id
        )
        return result is not False
    except Exception as e:
        logger.error(f'Translation failed for movie {radarr_id}: {e}')
        return False

def find_subtitle_by_language(subtitles, language_code, video_path, media_type='movie'):
    """Find a subtitle file by language code from the subtitles list."""
    available_subtitles = []

    if subtitles:
        # Parse subtitles if it's a string (Python literal from DB)
        if isinstance(subtitles, str):
            try:
                subtitles = ast.literal_eval(subtitles)
            except (ValueError, SyntaxError):
                logger.error('Failed to parse subtitles from database')
                subtitles = []

        if isinstance(subtitles, list):
            # Collect available subtitles with their paths for better processing
            for sub in subtitles:
                # DB format is [lang_str, path, size]
                if isinstance(sub, (list, tuple)) and len(sub) >= 2:
                    lang_parts = sub[0].split(':')
                    sub_code = lang_parts[0]
                    sub_path = sub[1]
                    sub_hi = len(lang_parts) > 1 and lang_parts[1].lower() == 'hi'
                    sub_forced = len(lang_parts) > 1 and lang_parts[1].lower() == 'forced'
                    
                    if sub_path:
                        available_subtitles.append({
                            'code2': sub_code,
                            'path': sub_path,
                            'hi': sub_hi,
                            'forced': sub_forced
                        })

    # Helper function to resolve and validate subtitle path
    def resolve_subtitle_path(sub_path):
        # Apply path mapping based on media type
        if media_type == 'series':
            mapped_path = path_mappings.path_replace(sub_path)
        else:
            mapped_path = path_mappings.path_replace_movie(sub_path)
        
        # Check if file exists at mapped path
        if os.path.exists(mapped_path):
            return mapped_path
        # Fallback to original path
        elif os.path.exists(sub_path):
            return sub_path
        
        return None

    # First pass: Look for exact language match in DB
    exact_matches = [s for s in available_subtitles if s['code2'] == language_code]
    
    # Sort matches: prefer non-HI, non-forced first, then HI, then forced
    exact_matches.sort(key=lambda x: (x['forced'], x['hi']))
    
    for sub in exact_matches:
        resolved_path = resolve_subtitle_path(sub['path'])
        if resolved_path:
            return resolved_path, sub['code2']

    # Second pass: If no exact match found in DB, try any available subtitle from DB
    if available_subtitles:
        # Sort all available: prefer non-HI, non-forced, and prioritize common languages
        common_languages = ['en', 'eng']  # English often has good quality subs
        
        def sort_key(sub):
            is_common = sub['code2'] in common_languages
            return (sub['forced'], sub['hi'], not is_common)
        
        available_subtitles.sort(key=sort_key)
        
        for sub in available_subtitles:
            resolved_path = resolve_subtitle_path(sub['path'])
            if resolved_path:
                return resolved_path, sub['code2']

    # Third pass: Scan filesystem fallback
    filesystem_subs = scan_filesystem_for_subtitles(video_path)
    
    if filesystem_subs:
        # Prefer English
        for sub in filesystem_subs:
            if sub['is_english']:
                return sub['path'], 'en'
        
        # Use first available
        sub = filesystem_subs[0]
        return sub['path'], sub['detected_language']
    
    return None, None

def scan_filesystem_for_subtitles(video_path):
    """Scan filesystem for .srt files next to the video file."""
    ENGLISH_PATTERNS = [
        r'\.en\.srt$', r'\.eng\.srt$', r'\.english\.srt$',
        r'[._-]en[._-]', r'[._-]eng[._-]', r'[._-]english[._-]',
    ]
    
    video_dir = os.path.dirname(video_path)
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    results = []
    
    # Search directories
    search_dirs = [video_dir]
    for subfolder in ['Subs', 'Subtitles', 'subs', 'subtitles', video_name]:
        subdir = os.path.join(video_dir, subfolder)
        if os.path.isdir(subdir):
            search_dirs.append(subdir)
    
    for directory in search_dirs:
        try:
            for filename in os.listdir(directory):
                if filename.lower().endswith('.srt'):
                    full_path = os.path.join(directory, filename)
                    
                    # Detect language from filename
                    is_english = any(re.search(p, filename.lower()) for p in ENGLISH_PATTERNS)
                    detected_lang = 'en' if is_english else detect_language_from_content(full_path)
                    
                    results.append({
                        'path': full_path,
                        'filename': filename,
                        'is_english': is_english or detected_lang == 'en',
                        'detected_language': detected_lang or 'und'
                    })
        except OSError:
            continue
    
    # Sort: English first
    results.sort(key=lambda x: (not x['is_english'], x['filename']))
    return results

def detect_language_from_content(srt_path):
    """Detect language by analyzing subtitle content."""
    from guess_language import guess_language
    from charset_normalizer import detect
    try:
        with open(srt_path, 'rb') as f:
            raw = f.read(8192)  # Read first 8KB
        
        encoding = detect(raw)
        if encoding and encoding.get('encoding'):
            text = raw.decode(encoding['encoding'], errors='ignore')
            return guess_language(text)
    except Exception:
        pass
    return None
