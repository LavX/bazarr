"""Exact local sports file identity, with no upstream-ID fallback."""
from dataclasses import dataclass

from sqlalchemy import select

from app.database import database, TableArrInstances, TableSportsEvents, TableSportsLeagues
from sportarr.parser import positive_id
from utilities.path_mappings import apply_sports_mapping, read_sports_mappings


@dataclass(frozen=True)
class SportsEventContext:
    event_id: int
    league_id: int
    arr_instance_id: int
    upstream_event_id: int
    upstream_league_id: int
    file_id: int
    original_path: str
    mapped_path: str
    profile_id: int | None


def resolve_event(event_id, arr_instance_id=None, enabled_only=True):
    return resolve_event_in_session(database, event_id, arr_instance_id, enabled_only)


def resolve_event_in_session(session, event_id, arr_instance_id=None, enabled_only=True):
    if not positive_id(event_id) or arr_instance_id is not None and not positive_id(arr_instance_id):
        raise ValueError('An exact local event ID and valid owner are required')
    query = select(TableSportsEvents, TableSportsLeagues, TableArrInstances).join(
        TableSportsLeagues, (TableSportsEvents.league_id == TableSportsLeagues.id) &
        (TableSportsEvents.arr_instance_id == TableSportsLeagues.arr_instance_id)).join(
        TableArrInstances, TableSportsEvents.arr_instance_id == TableArrInstances.id).where(
        TableSportsEvents.id == event_id, TableArrInstances.kind == 'sportarr')
    if arr_instance_id is not None:
        query = query.where(TableArrInstances.id == arr_instance_id)
    if enabled_only:
        query = query.where(TableArrInstances.enabled == 1)
    found = session.execute(query.execution_options(populate_existing=True)).first()
    if found is None:
        raise ValueError('Sports event not found for this owner')
    event, league, instance = found
    return SportsEventContext(event.id, league.id, instance.id, event.sportarrEventId,
                              league.sportarrLeagueId, event.file_id, event.path,
                              apply_sports_mapping(event.path, read_sports_mappings(instance.path_mappings)),
                              league.profileId)
