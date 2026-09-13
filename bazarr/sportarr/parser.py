"""Strict native event parsing, one record for each playable file."""
from constants import MINIMUM_VIDEO_SIZE


def positive_id(value):
    return type(value) is int and value > 0


def _nullable_number(value):
    return value is None or type(value) is int and value >= 0


def parse_events(events, upstream_league_id):
    event_ids, file_ids, parts, paths = set(), set(), set(), set()
    result = []
    for event in events:
        if (not isinstance(event, dict) or not positive_id(event.get('id'))
                or event.get('leagueId') != upstream_league_id
                or type(event.get('leagueId')) is not int
                or not isinstance(event.get('title'), str) or not event['title'].strip()
                or type(event.get('monitored')) is not bool
                or not isinstance(event.get('files'), list)
                or not isinstance(event.get('eventDate'), str) or not event['eventDate'].strip()
                or any(event.get(key) is not None and not isinstance(event[key], str)
                       for key in ('broadcastDate', 'externalId'))
                or not _nullable_number(event.get('seasonNumber'))
                or not _nullable_number(event.get('episodeNumber'))):
            raise ValueError('Malformed Sportarr event')
        if event['id'] in event_ids:
            raise ValueError('Duplicate Sportarr event identity')
        event_ids.add(event['id'])
        for file in event['files']:
            if (not isinstance(file, dict) or not positive_id(file.get('id'))
                    or type(file.get('eventId')) is not int or file['eventId'] != event['id']
                    or not isinstance(file.get('filePath'), str) or not file['filePath'].strip()
                    or type(file.get('size')) is not int or file['size'] < 0
                    or type(file.get('exists')) is not bool
                    or not _nullable_number(file.get('partNumber'))
                    or not isinstance(file.get('languages', []), list)
                    or any(not isinstance(lang, str) for lang in file.get('languages', []))
                    or any(file.get(key) is not None and not isinstance(file[key], str)
                           for key in ('quality', 'releaseTitle', 'codec', 'audioCodec', 'partName'))):
                raise ValueError('Malformed Sportarr event file')
            part = file.get('partNumber') or 0
            key = (event['id'], part)
            if file['id'] in file_ids or key in parts or file['filePath'] in paths:
                raise ValueError('Duplicate or conflicting Sportarr file identity')
            file_ids.add(file['id'])
            parts.add(key)
            paths.add(file['filePath'])
            if not file['exists'] or file['size'] <= MINIMUM_VIDEO_SIZE:
                continue
            quality = (file.get('quality') or '').split('-', 1)
            result.append(dict(
                sportarrEventId=event['id'], sportarrLeagueId=upstream_league_id,
                externalId=event.get('externalId'), title=event['title'],
                path=file['filePath'], season=event.get('seasonNumber'),
                episode=event.get('episodeNumber'), eventDate=event.get('eventDate'),
                broadcastDate=event.get('broadcastDate'), partNumber=part,
                partName=file.get('partName'), sceneName=file.get('releaseTitle'),
                monitored=str(event['monitored']), format=quality[0] or None,
                resolution=quality[1] if len(quality) == 2 else None,
                video_codec=file.get('codec'), audio_codec=file.get('audioCodec'),
                audio_language=str(file.get('languages', [])), file_id=file['id'],
                file_size=file['size']))
    return result
