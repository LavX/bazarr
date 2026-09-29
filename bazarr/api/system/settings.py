# coding=utf-8

import json
import logging

from flask import request, jsonify
from flask_restx import Resource, Namespace
from dynaconf.validator import ValidationError

from api.utils import None_Keys
from app.database import TableLanguagesProfiles, TableSettingsLanguages, TableSettingsNotifier, \
    normalize_profile_items, update_profile_id_list, database, insert, update, delete, select
from app.event_handler import event_stream
from app.config import (save_settings, get_settings, validate_metadata_settings,
                        MetadataPersistenceError, MetadataFollowupError, SettingsFollowupError)
from app.scheduler import scheduler  # noqa: F401
from subtitles.indexer.missing_refresh import queue_missing_subtitles_recalculation
from subtitles.language_profiles import validate_combine_rule, CombineRuleError
from arr_instances.resolution import forget_deleted_language_profiles

from ..utils import authenticate

api_ns_system_settings = Namespace('systemSettings', description='System settings API endpoint')


def _write_settings_rows(enabled_languages, profiles, notifications):
    """Write the database part of a settings save, once its configuration is saved.

    The rows only. The events and the queued work that follow them are left to
    _after_settings_rows, so an event that cannot go out no longer stops the
    rows after it.
    """
    if len(enabled_languages) != 0:
        database.execute(
            update(TableSettingsLanguages)
            .values(enabled=0))
        for code in enabled_languages:
            database.execute(
                update(TableSettingsLanguages)
                .values(enabled=1)
                .where(TableSettingsLanguages.code2 == code))

    deleted_profile_ids = []
    if profiles is not None:
        existing_ids = database.execute(
            select(TableLanguagesProfiles.profileId))\
            .all()
        existing = [x.profileId for x in existing_ids]
        for item in profiles:
            combine_rule = item.get('combine')
            combine_value = json.dumps(combine_rule) if combine_rule else None
            # A client may omit the optional per-language keys, and the
            # migration that adds them runs at startup only. Storing the
            # item as sent left every indexing pass raising KeyError on it
            # until the next restart, so fill them in here instead.
            normalize_profile_items(item['items'])
            if item['profileId'] in existing:
                # Update existing profiles
                database.execute(
                    update(TableLanguagesProfiles)
                    .values(
                        name=item['name'],
                        cutoff=item['cutoff'] if item['cutoff'] not in None_Keys else None,
                        items=json.dumps(item['items']),
                        mustContain=str(item['mustContain']),
                        mustNotContain=str(item['mustNotContain']),
                        originalFormat=int(item['originalFormat']) if item['originalFormat'] not in None_Keys else
                        None,
                        tag=item['tag'] if 'tag' in item else None,
                        combine=combine_value,
                    )
                    .where(TableLanguagesProfiles.profileId == item['profileId']))
                existing.remove(item['profileId'])
            else:
                # Add new profiles
                database.execute(
                    insert(TableLanguagesProfiles)
                    .values(
                        profileId=item['profileId'],
                        name=item['name'],
                        cutoff=item['cutoff'] if item['cutoff'] not in None_Keys else None,
                        items=json.dumps(item['items']),
                        mustContain=str(item['mustContain']),
                        mustNotContain=str(item['mustNotContain']),
                        originalFormat=int(item['originalFormat']) if item['originalFormat'] not in None_Keys else
                        None,
                        tag=item['tag'] if 'tag' in item else None,
                        combine=combine_value,
                    ))
        for profileId in existing:
            # Remove deleted profiles
            database.execute(
                delete(TableLanguagesProfiles)
                .where(TableLanguagesProfiles.profileId == profileId))
        deleted_profile_ids = list(existing)

        # invalidate cache
        update_profile_id_list.invalidate()

    # Update Notification
    for item in notifications:
        database.execute(
            update(TableSettingsNotifier).values(
                enabled=int(item['enabled'] is True),
                url=item['url'])
            .where(TableSettingsNotifier.name == item['name']))

    # Every stored reference to a profile that was just deleted. The
    # editor allocates a new id as max(existing) + 1, so deleting the
    # highest-numbered profile and adding another reuses that id, and a
    # reference left behind would point at an unrelated profile that
    # does exist, which no validation downstream can catch.
    #
    # After save_settings, not before: this reads the configuration, and
    # a user who picks a replacement default and deletes the old profile
    # in the same Apply has the replacement only in the request. Running
    # first would see the profile being replaced, switch the default off,
    # and the untouched enable checkbox is not in the form to turn it
    # back on.
    forget_deleted_language_profiles(deleted_profile_ids)


def _best_effort(action, *args):
    """Run one step that follows a written save, and report whether it went through.

    A step went through unless it raised or returned False.
    """
    try:
        return action(*args) is not False
    except Exception:
        logging.exception("Settings were saved, but a step after saving them failed")
        return False


def _queue_recalculation():
    """Queue the library-wide missing subtitles recalculation, and say whether it was queued.

    The helper never raises: it logs its own failure and returns None, which for
    a library-wide pass means no job was queued or joined.
    """
    return queue_missing_subtitles_recalculation() is not None


def _after_settings_rows(enabled_languages, profiles):
    """Announce a save whose configuration and rows are written, and queue its work.

    Every step runs whichever other one fails, and none raises: the save is kept
    by now, and Socket.IO refuses every event while its transport is down, which
    must not replace the answer that says so. Returns whether all of them went
    through.
    """
    done = []
    if len(enabled_languages) != 0 or profiles is not None:
        done.append(_best_effort(event_stream, "languages"))

    # Recalculated by a queued job, not here: a library-wide pass inside the
    # request is what made the save slow enough for a proxy to time it out
    # and report a save that went through as "Save failed". After the
    # settings, so the job reads the arr toggles this same save may have
    # changed. The response does not wait for it.
    if profiles is not None:
        done.append(_best_effort(_queue_recalculation))

    # Other open pages reload the settings, as they do after any written save.
    done.append(_best_effort(event_stream, "settings"))
    return all(done)


@api_ns_system_settings.hide
@api_ns_system_settings.route('system/settings')
class SystemSettings(Resource):
    @authenticate
    def get(self):
        data = get_settings()
        data['notifications'] = dict()
        data['notifications']['providers'] = [{
            'name': x.name,
            'enabled': x.enabled == 1,
            'url': x.url
        } for x in database.execute(
            select(TableSettingsNotifier.name,
                   TableSettingsNotifier.enabled,
                   TableSettingsNotifier.url)
            .order_by(TableSettingsNotifier.name))
            .all()]

        return jsonify(data)

    @authenticate
    def post(self):
        try:
            validate_metadata_settings(list(zip(request.form.keys(), request.form.listvalues())))
        except ValidationError as error:
            return error.message, 406
        # Everything the request writes to the database is read and checked
        # here and written only once the configuration is on disk. The rows
        # used to go first, and they are not in any transaction the save can
        # undo, so a save refused for its configuration still kept its
        # languages, profiles and notifiers while the user was told it failed.
        enabled_languages = request.form.getlist('languages-enabled')
        languages_profiles = request.form.get('languages-profiles')
        profiles = json.loads(languages_profiles) if languages_profiles else None
        for item in profiles or []:
            # Validate the combine rule at save time and reject it, instead of
            # storing an invalid rule that get_combine_rule then silently drops
            # (returns None), leaving the profile looking configured while
            # auto-combine never runs.
            combine_rule = item.get('combine')
            if combine_rule:
                try:
                    validate_combine_rule(combine_rule, item.get('items') or [])
                except CombineRuleError as error:
                    return f"Invalid combine rule for profile '{item.get('name')}': {error}", 400
        notifications = [json.loads(item) for item in request.form.getlist('notifications-providers')]

        try:
            save_settings(zip(request.form.keys(), request.form.listvalues()))
        except MetadataPersistenceError:
            return "Metadata settings could not be saved. Try again.", 503
        except ValidationError as e:
            try:
                event_stream("settings")
            except Exception:
                # Best effort: nothing was saved, and the answer has to say so.
                logging.exception("Settings were refused, and announcing that failed")
            return e.message, 406
        except SettingsFollowupError as error:
            # The configuration did reach the disk; only the refresh after it
            # failed. Its rows follow it, as they do for any saved change.
            refresh_error = error
        else:
            refresh_error = None

        _write_settings_rows(enabled_languages, profiles, notifications)
        applied = _after_settings_rows(enabled_languages, profiles)
        if isinstance(refresh_error, MetadataFollowupError):
            return {"code": "discover_settings_refresh_failed",
                    "message": "Metadata settings were saved, but application refresh failed. Reload settings before retrying."}, 503
        if refresh_error is not None or not applied:
            return {"code": "settings_refresh_failed",
                    "message": "Settings were saved, but applying them failed. Reload settings before retrying."}, 503
        return '', 204


@api_ns_system_settings.route('system/webhooks/test')
class SystemWebhookTest(Resource):
    @authenticate
    def post(self):
        """Test external webhook connection."""
        try:
            from utilities.autopulse_webhook import test_external_webhook_connection
            
            success, message = test_external_webhook_connection()
            
            return {
                'data': {
                    'success': success,
                    'message': message
                }
            }
            
        except Exception as e:
            return {
                'data': {
                    'success': False,
                    'message': f'Failed to test webhook: {str(e)}'
                }
            }, 400
