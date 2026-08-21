"""Public-facing HTTP contract routes for the Z-SPAN flagship."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
from datetime import datetime, timezone
from functools import wraps
from typing import Any
from uuid import UUID

import requests
from flask import Blueprint, current_app, jsonify, request

try:
    from parsers import national_catalog, public_dto
except ImportError:  # Direct import from parsers/.
    import national_catalog
    import public_dto

__all__ = ["bp"]

bp = Blueprint("public_api", __name__)

_API_SERVER_DEPENDENCIES = (
    "_COVERAGE_PUBLIC_LABELS",
    "_PUBLIC_YOUTUBE_EMBED_CACHE_TTL_SECONDS",
    "_PUBLIC_YOUTUBE_EMBED_ERROR_CACHE_TTL_SECONDS",
    "_build_citation_tree",
    "_ccta_word_timings",
    "_channel_city_identity",
    "_channel_county_name",
    "_channel_date_bound",
    "_channel_state_name",
    "_channel_status",
    "_extract_year",
    "_genericize_speaker_attribution",
    "_genericize_speaker_attribution_in_content",
    "_load_city_intelligence",
    "_load_verified_key_decisions",
    "_materialize_decision_excerpts_for_response",
    "_project_public_dto",
    "_public_calendar_search_pagination",
    "_public_cast_member",
    "_public_episode_card",
    "_public_int_arg",
    "_public_json_list",
    "_public_word_timings",
    "_require_owner",
    "_resolve_visible_public_meeting",
    "_role_sort_key",
    "_youtube_embed_cache_get",
    "_youtube_embed_cache_put",
    "app",
    "count_users",
    "get_connection",
    "public_serving_sql",
)


def _api_server_module():
    """Resolve the hosting api_server lazily, after its import is complete."""
    module = sys.modules.get(current_app.import_name)
    if module is None or not hasattr(module, "_public_rate_limited"):
        module = importlib.import_module("api_server")
    return module


def _bind_api_server_dependencies(module) -> None:
    """Bind shared helpers only when a public request is dispatched."""
    globals().update({
        name: getattr(module, name)
        for name in _API_SERVER_DEPENDENCIES
    })


def _public_rate_limited(route_family: str):
    """Apply api_server's canonical limiter lazily without a circular import."""
    def decorator(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            module = _api_server_module()
            _bind_api_server_dependencies(module)
            limited_handler = module._public_rate_limited(route_family)(handler)
            return limited_handler(*args, **kwargs)
        return wrapped
    return decorator


@bp.route('/public-api/channels/tree', methods=['GET'])
@_public_rate_limited('public_read')
def public_channels_tree():
    requested_state = str(request.args.get("state") or "").strip().upper()
    if requested_state and not re.fullmatch(r"[A-Z]{2}", requested_state):
        return jsonify({"error": "state must be a two-letter code"}), 400

    conn = get_connection()
    try:
        # Directory semantics (operator-directed 2026-08-05; restored
        # 2026-08-13): every public-safe roster city appears in the tree, but
        # ALL counts/dates aggregate over publicly-served rows only. A city
        # with zero published meetings therefore emits 0/0 as `scaffold`;
        # the client presents that honest-empty shelf as amber and clickable.
        # City NAMES are public coverage status (allowed under the D-153
        # public-coverage carve-out); nothing about unpublished meetings
        # (counts, dates, titles) crosses the boundary.
        _serving = (
            "m.is_published = 1 AND EXISTS ("
            "SELECT 1 FROM work_orders w "
            "WHERE w.meeting_id = m.id AND w.approved_at IS NOT NULL)"
        )
        rows = conn.execute(
            f"""
            SELECT m.state, m.county, m.city_name,
                   SUM(CASE WHEN {_serving} THEN 1 ELSE 0 END) AS meeting_count,
                   SUM(CASE WHEN {_serving} AND EXISTS (
                       SELECT 1 FROM notebook_outputs no
                       WHERE no.meeting_id = m.id
                         AND no.output_type IN ({','.join('?' for _ in public_dto.PUBLIC_BROADCAST_OUTPUT_TYPES)})
                         AND COALESCE(no.content, '') != ''
                         AND no.voided_at IS NULL
                   ) THEN 1 ELSE 0 END) AS broadcast_count,
                   MAX(CASE WHEN {_serving} THEN m.meeting_date END) AS last_meeting,
                   MIN(CASE WHEN {_serving} THEN m.meeting_date END) AS first_meeting
            FROM meetings m
            WHERE m.city_name IS NOT NULL AND m.city_name != ''
            GROUP BY m.state, m.county, m.city_name
            ORDER BY m.state, m.county, m.city_name
            """,
            public_dto.PUBLIC_BROADCAST_OUTPUT_TYPES,
        ).fetchall()
        roster_rows = conn.execute(
            """
            SELECT state, county, name AS city_name
            FROM cities
            WHERE TRIM(COALESCE(name, '')) != ''
              AND TRIM(COALESCE(county, '')) != ''
              AND TRIM(COALESCE(state, '')) != ''
            ORDER BY state, county, name
            """
        ).fetchall()
    finally:
        conn.close()

    states: list[dict] = []
    state_nodes: dict[str, dict] = {}
    county_nodes: dict[tuple[str, str], dict] = {}
    city_nodes: dict[tuple[str, ...], dict] = {}
    route_nodes: dict[str, list[dict]] = {}

    def ensure_state(state: str) -> tuple[str, dict]:
        state_key = state.casefold()
        if state_key not in state_nodes:
            state_node = _project_public_dto(
                {
                    'state': state,
                    'statewide_sources': [],
                    'regional_sources': [],
                    'counties': [],
                },
                public_dto.PUBLIC_CHANNEL_STATE_FIELDS,
            )
            state_nodes[state_key] = state_node
            states.append(state_node)
        return state_key, state_nodes[state_key]

    def ensure_county(state: str, county: str) -> tuple[str, str, dict]:
        state_key, state_node = ensure_state(state)
        county_identity = county.casefold()
        county_key = (state_key, county_identity)
        if county_key not in county_nodes:
            county_node = _project_public_dto(
                {'county': county, 'sources': [], 'cities': []},
                public_dto.PUBLIC_CHANNEL_COUNTY_FIELDS,
            )
            county_nodes[county_key] = county_node
            state_node['counties'].append(county_node)
        return state_key, county_identity, county_nodes[county_key]

    def refresh_status(city: dict) -> None:
        if city['broadcast_count'] > 0:
            city['status'] = 'live'
        elif city['meeting_count'] > 0 or city['source_status'] != 'needs_source':
            city['status'] = 'cached'
        else:
            city['status'] = 'scaffold'

    # A state-scoped request overlays the manually imported National Civics
    # Catalog roster. The unscoped route retains its legacy DB-only response
    # for older clients; the current client requests one state at a time.
    catalog_state = None
    if requested_state:
        catalog_state = national_catalog.state_roster(requested_state)
        if catalog_state is not None:
            state_name = catalog_state['name']
            for place in catalog_state['places']:
                state_key, state_node = ensure_state(state_name)
                place_type = place['place_type'].casefold()
                # A state tab already represents the geographic state. The
                # roster's generic state placeholder is not itself a meeting
                # body; future state-level shelves must name the legislature,
                # commission, board, or other body they actually represent.
                if place_type == 'state':
                    continue
                source_id = place['source_id']
                city = _project_public_dto({
                    'source_id': source_id,
                    'name': place['name'],
                    'place_type': place['place_type'],
                    'route_name': place['route_name'] or '',
                    'source_status': place['status'],
                    'contribution_url': national_catalog.contribution_url(
                        place, state_code=catalog_state['code'],
                    ),
                    'meeting_count': 0,
                    'broadcast_count': 0,
                    'status': 'scaffold',
                    'last_meeting': '',
                    'first_meeting': '',
                    'lat': None,
                    'lng': None,
                }, public_dto.PUBLIC_CHANNEL_CITY_FIELDS)
                refresh_status(city)
                county = place['county_name']
                if county.casefold() == 'statewide and regional':
                    city_key = (state_key, 'regional', source_id)
                    state_node['regional_sources'].append(city)
                else:
                    state_key, county_identity, county_node = ensure_county(
                        state_name, county
                    )
                    bucket = 'county-source' if place_type == 'county' else 'place'
                    city_key = (state_key, county_identity, bucket, source_id)
                    if place_type == 'county':
                        county_node['sources'].append(city)
                    else:
                        county_node['cities'].append(city)
                city_nodes[city_key] = city
                route_name = city['route_name'].casefold()
                if route_name:
                    route_nodes.setdefault(route_name, []).append(city)

    for raw in rows:
        row = dict(raw)
        state = _channel_state_name(row.get('state'))
        county = _channel_county_name(row.get('county'))
        city_name = ' '.join(str(row.get('city_name') or '').split())
        meeting_count = int(row.get('meeting_count') or 0)
        broadcast_count = int(row.get('broadcast_count') or 0)
        routed = route_nodes.get(city_name.casefold(), [])
        if routed:
            for existing_city in routed:
                existing_city['meeting_count'] += meeting_count
                existing_city['broadcast_count'] += broadcast_count
                refresh_status(existing_city)
                existing_city['last_meeting'] = _channel_date_bound(
                    existing_city['last_meeting'], row.get('last_meeting') or '', latest=True,
                ) or ''
                existing_city['first_meeting'] = _channel_date_bound(
                    existing_city['first_meeting'], row.get('first_meeting') or '', latest=False,
                ) or ''
            continue

        if requested_state and (
            catalog_state is None
            or state.casefold() != catalog_state['name'].casefold()
        ):
            continue
        state_key, county_identity, county_node = ensure_county(state, county)
        city_key = (state_key, county_identity, f"legacy:{city_name.casefold()}")
        if city_key in city_nodes:
            continue
        city = _project_public_dto({
            'source_id': '',
            'name': city_name,
            'place_type': 'municipality',
            'route_name': city_name,
            'source_status': 'unverified',
            'contribution_url': '',
            'meeting_count': meeting_count,
            'broadcast_count': broadcast_count,
            'status': _channel_status(meeting_count, broadcast_count, False),
            'last_meeting': row.get('last_meeting') or '',
            'first_meeting': row.get('first_meeting') or '',
            'lat': None,
            'lng': None,
        }, public_dto.PUBLIC_CHANNEL_CITY_FIELDS)
        county_node['cities'].append(city)
        city_nodes[city_key] = city
        route_nodes.setdefault(city_name.casefold(), []).append(city)

    # Add the public-safe jurisdiction roster after the published-meeting
    # aggregation. Only identity crosses this boundary: no calendar URL,
    # parser filename, vendor, private routing, or health note.
    # Existing meeting-derived rows win; roster-only cities are honest 0/0
    # scaffolds that the client presents as amber and clickable.
    for raw in roster_rows:
        row = dict(raw)
        raw_state = ' '.join(str(row.get('state') or '').split())
        raw_county = ' '.join(str(row.get('county') or '').split())
        city_name = ' '.join(str(row.get('city_name') or '').split())
        if not raw_state or not raw_county or not city_name:
            continue
        if raw_state.casefold() == 'unknown' or raw_county.casefold() == 'unknown':
            continue
        state = _channel_state_name(raw_state)
        county = _channel_county_name(raw_county)
        routed = route_nodes.get(city_name.casefold(), [])
        if routed:
            continue
        if requested_state and (
            catalog_state is None
            or state.casefold() != catalog_state['name'].casefold()
        ):
            continue
        state_key, county_identity, county_node = ensure_county(state, county)
        city_key = (state_key, county_identity, f"legacy:{city_name.casefold()}")
        if city_key in city_nodes:
            continue
        city = _project_public_dto({
            'source_id': '',
            'name': city_name,
            'place_type': 'municipality',
            'route_name': city_name,
            'source_status': 'unverified',
            'contribution_url': '',
            'meeting_count': 0,
            'broadcast_count': 0,
            'status': 'scaffold',
            'last_meeting': '',
            'first_meeting': '',
            'lat': None,
            'lng': None,
        }, public_dto.PUBLIC_CHANNEL_CITY_FIELDS)
        county_node['cities'].append(city)
        city_nodes[city_key] = city
        route_nodes.setdefault(city_name.casefold(), []).append(city)

    for state in states:
        state['statewide_sources'].sort(key=lambda item: item['name'].casefold())
        state['regional_sources'].sort(key=lambda item: item['name'].casefold())
        state['counties'].sort(key=lambda item: item['county'].casefold())
        for county in state['counties']:
            county['sources'].sort(key=lambda item: item['name'].casefold())
            county['cities'].sort(key=lambda item: item['name'].casefold())
    states.sort(key=lambda item: item['state'].casefold())
    return jsonify(_project_public_dto(
        {'ok': True, 'states': states}, public_dto.PUBLIC_CHANNELS_TREE_FIELDS,
    ))


@bp.route('/public-api/catalog/contribute/<source_id>.md', methods=['GET'])
@_public_rate_limited('public_read')
def public_catalog_contribution_handoff(source_id):
    """Serve one copy-ready Markdown handoff instead of a raw JSONL line."""
    if not national_catalog.SOURCE_ID_RE.fullmatch(source_id):
        return jsonify({'error': 'catalog source not found'}), 404
    state_code = str(request.args.get('state') or '').strip().upper()
    if not re.fullmatch(r'[A-Z]{2}', state_code):
        return jsonify({'error': 'state must be a two-letter code'}), 400
    resolved = national_catalog.source_projection(
        source_id, state_code=state_code,
    )
    if resolved is None:
        return jsonify({'error': 'catalog source not found'}), 404
    state, place = resolved
    roster = national_catalog.load_roster()
    markdown = national_catalog.contribution_handoff_markdown(
        state, place, commit=roster['catalog_commit'],
    )
    response = current_app.response_class(
        markdown, status=200, mimetype='text/markdown',
    )
    response.headers['Content-Disposition'] = (
        f'inline; filename="help-{source_id}.md"'
    )
    response.headers['Cache-Control'] = 'public, max-age=300'
    return response


@bp.route('/public-api/cities/<city_name>/years', methods=['GET'])
@_public_rate_limited('public_read')
def public_city_years(city_name):
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT m.meeting_date FROM meetings m
                WHERE m.city_name COLLATE NOCASE = ?{public_serving_sql('m')}""",
            (city_name,),
        ).fetchall()
    finally:
        conn.close()
    years = sorted(
        {year for row in rows if (year := _extract_year(row['meeting_date']))},
        reverse=True,
    )
    return jsonify(_project_public_dto({
        'ok': True,
        'city': city_name,
        'years': years,
        'current_year': str(datetime.now().year),
    }, public_dto.PUBLIC_CITY_YEARS_FIELDS))


@bp.route('/public-api/cities/<city_name>/meetings', methods=['GET'])
@_public_rate_limited('public_read')
def public_city_meetings(city_name):
    year_arg = (request.args.get('year') or '').strip()
    conditions = ['m.city_name COLLATE NOCASE = ?']
    params: list[Any] = [city_name]
    if year_arg and year_arg.lower() != 'all':
        conditions.append('m.meeting_date LIKE ?')
        params.append(f'%{year_arg}%')
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT m.public_id, m.city_name, m.county, m.state,
                   m.meeting_title, m.meeting_date, m.meeting_time,
                   m.meeting_location, m.meeting_status, m.agenda_url,
                   m.minutes_url, m.agenda_packet_url, m.video_url,
                   m.ecomment_url, m.published_at,
                   (SELECT no.content FROM notebook_outputs no
                    WHERE no.meeting_id = m.id
                      AND no.output_type = 'episode_tagline'
                      AND no.voided_at IS NULL
                    LIMIT 1) AS episode_tagline
            FROM meetings m
            WHERE {' AND '.join(conditions)}{public_serving_sql('m')}
            ORDER BY m.meeting_date DESC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    events = [_public_episode_card(dict(row)) for row in rows]
    return jsonify(_project_public_dto({
        'success': True,
        'city': city_name,
        'year': year_arg or str(datetime.now().year),
        'count': len(events),
        'events': events,
    }, public_dto.PUBLIC_CITY_MEETINGS_FIELDS))


@bp.route('/public-api/calendar/county/<county_name>/meetings', methods=['GET'])
@_public_rate_limited('public_read')
def public_county_meetings(county_name):
    state = (request.args.get('state') or 'Arizona').strip()
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT m.public_id, m.city_name AS city, m.county,
                   m.meeting_title, m.meeting_date, m.meeting_time,
                   m.meeting_location, m.meeting_status, m.agenda_url,
                   m.minutes_url, m.video_url
            FROM meetings m
            WHERE m.county COLLATE NOCASE = ?
              AND m.state COLLATE NOCASE = ?{public_serving_sql('m')}
            ORDER BY m.city_name, m.meeting_date DESC
            """,
            (county_name, state),
        ).fetchall()
    finally:
        conn.close()
    cities: dict[str, list[dict]] = {}
    for raw in rows:
        row = dict(raw)
        city = row.get('city') or ''
        cities.setdefault(city, []).append(_project_public_dto(
            row, public_dto.PUBLIC_COUNTY_MEETING_FIELDS,
        ))
    return jsonify(_project_public_dto({
        'success': True,
        'county': county_name,
        'total_meetings': sum(len(value) for value in cities.values()),
        'cities': cities,
    }, public_dto.PUBLIC_COUNTY_MEETINGS_FIELDS))


@bp.route('/public-api/calendar/search', methods=['GET'])
@_public_rate_limited('public_search')
def public_calendar_search():
    query = (request.args.get('q') or '').strip()
    conditions = ['1=1']
    params: list[Any] = []
    if query:
        pattern = f'%{query}%'
        conditions.append(
            "(m.meeting_title LIKE ? COLLATE NOCASE OR "
            "m.city_name LIKE ? COLLATE NOCASE OR "
            "m.county LIKE ? COLLATE NOCASE OR "
            "m.meeting_location LIKE ? COLLATE NOCASE)"
        )
        params.extend((pattern, pattern, pattern, pattern))
    for argument, column in (('county', 'county'), ('state', 'state')):
        value = (request.args.get(argument) or '').strip()
        if value:
            conditions.append(f'm.{column} COLLATE NOCASE = ?')
            params.append(value)
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()
    if date_from:
        conditions.append('m.meeting_date >= ?')
        params.append(date_from)
    if date_to:
        conditions.append('m.meeting_date <= ?')
        params.append(date_to)
    limit, offset = _public_calendar_search_pagination()
    where = ' AND '.join(conditions) + public_serving_sql('m')
    conn = get_connection()
    try:
        total = int(conn.execute(
            f'SELECT COUNT(*) FROM meetings m WHERE {where}', params,
        ).fetchone()[0])
        rows = conn.execute(
            f"""
            SELECT m.public_id, m.city_name AS city, m.county, m.state,
                   m.meeting_title, m.meeting_date, m.meeting_time,
                   m.meeting_location, m.meeting_status, m.agenda_url,
                   m.minutes_url, m.video_url
            FROM meetings m WHERE {where}
            ORDER BY m.meeting_date DESC LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
    finally:
        conn.close()
    results = [
        _project_public_dto(dict(row), public_dto.PUBLIC_SEARCH_RESULT_FIELDS)
        for row in rows
    ]
    return jsonify(_project_public_dto({
        'success': True,
        'results': results,
        'total': total,
        'limit': limit,
        'offset': offset,
        'has_more': offset + limit < total,
    }, public_dto.PUBLIC_SEARCH_FIELDS))


@bp.route('/public-api/calendar/stats', methods=['GET'])
@_public_rate_limited('public_search')
def public_calendar_stats():
    gate = public_serving_sql('m')
    conn = get_connection()
    try:
        total_meetings = int(conn.execute(
            f'SELECT COUNT(*) FROM meetings m WHERE 1=1{gate}'
        ).fetchone()[0])
        total_cities = int(conn.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM meetings m WHERE 1=1{gate} "
            "GROUP BY m.state, m.county, m.city_name)"
        ).fetchone()[0])
        states = [row[0] for row in conn.execute(
            f'SELECT DISTINCT m.state FROM meetings m WHERE 1=1{gate} ORDER BY m.state'
        ).fetchall()]
        counties = [row[0] for row in conn.execute(
            f'SELECT DISTINCT m.county FROM meetings m WHERE 1=1{gate} ORDER BY m.county'
        ).fetchall()]
        county_rows = conn.execute(
            f"SELECT m.county, COUNT(*) AS n FROM meetings m WHERE 1=1{gate} "
            "GROUP BY m.county ORDER BY n DESC"
        ).fetchall()
        city_rows = conn.execute(
            f"SELECT m.city_name AS city, m.county, COUNT(*) AS meetings "
            f"FROM meetings m WHERE 1=1{gate} GROUP BY m.city_name, m.county "
            "ORDER BY meetings DESC LIMIT 20"
        ).fetchall()
    finally:
        conn.close()
    top_cities = [_project_public_dto(
        dict(row), public_dto.PUBLIC_CALENDAR_TOP_CITY_FIELDS,
    ) for row in city_rows]
    return jsonify(_project_public_dto({
        'total_cities': total_cities,
        'total_meetings': total_meetings,
        'states': states,
        'counties': counties,
        'meetings_by_county': {row['county']: row['n'] for row in county_rows},
        'top_cities': top_cities,
    }, public_dto.PUBLIC_CALENDAR_STATS_FIELDS))


@bp.route('/public-api/health', methods=['GET'])
@_public_rate_limited('public_read')
def public_health():
    return jsonify(_project_public_dto(
        {'status': 'ok'}, public_dto.PUBLIC_HEALTH_FIELDS,
    ))


@bp.route('/public-api/broadcasts/<public_id>', methods=['GET'])
@_public_rate_limited('public_read')
def public_broadcast(public_id):
    resolved = _resolve_visible_public_meeting(public_id)
    if resolved is None:
        return jsonify({'success': False, 'error': 'broadcast not found'}), 404
    meeting, meeting_id = resolved
    conn = get_connection()
    try:
        work_order = conn.execute(
            """SELECT approved_at, youtube_video_url FROM work_orders
               WHERE meeting_id = ? AND approved_at IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (meeting_id,),
        ).fetchone()
        database_output_types = tuple(
            output_type
            for output_type in public_dto.PUBLIC_BROADCAST_OUTPUT_TYPES
            if output_type != 'key_decisions'
        ) + ('transcript_words',)
        rows = conn.execute(
            f"""SELECT output_type, content, voided_at FROM notebook_outputs
                WHERE meeting_id = ?
                  AND output_type IN ({','.join('?' for _ in database_output_types + ('key_decisions',))})""",
            (meeting_id, *database_output_types, 'key_decisions'),
        ).fetchall()
    finally:
        conn.close()
    by_type = {
        row['output_type']: row['content'] or ''
        for row in rows
        if row['voided_at'] is None
    }
    voided_types = {
        row['output_type']
        for row in rows
        if row['voided_at'] is not None
    }
    ccta_word_timings = _ccta_word_timings(
        by_type.get('community_calls_to_action'),
        by_type.get('transcript_words'),
    )
    outputs = {
        output_type: _project_public_dto(
            {
                'content': _genericize_speaker_attribution_in_content(
                    by_type.get(output_type, '')
                ),
                'karaoke_word_timings': (
                    ccta_word_timings
                    if output_type == 'community_calls_to_action'
                    else None
                ),
            },
            public_dto.PUBLIC_BROADCAST_OUTPUT_FIELDS,
        )
        for output_type in public_dto.PUBLIC_BROADCAST_OUTPUT_TYPES
        # D-157 display-cut (operator-directed 2026-07-27 session-97): synopsis withheld from the public plane until it clears the auditor re-entry bar (10 consecutive green runs, zero factual/causal/citation defects). Keep generating; hide-not-delete.
        if output_type not in {'synopsis', 'key_decisions'} and output_type in by_type
    }
    verified_key_decisions = (
        _load_verified_key_decisions(meeting_id)
        if 'key_decisions' in by_type
        else None
    )
    if verified_key_decisions is not None:
        outputs['key_decisions'] = _project_public_dto(
            {
                'content': _genericize_speaker_attribution_in_content(
                    verified_key_decisions
                ),
            },
            public_dto.PUBLIC_BROADCAST_OUTPUT_FIELDS,
        )
    from database import check_publish_readiness  # noqa: PLC0415
    verdict = check_publish_readiness(meeting_id)
    required_ok = verdict.get('required_ok')
    if (
        verified_key_decisions is None
        and 'key_decisions' not in voided_types
        and 'key_decisions' not in (verdict.get('missing_outputs') or [])
        and isinstance(required_ok, int)
    ):
        required_ok = max(0, required_ok - 1)
    completeness = _project_public_dto({
        'complete': (
            bool(verdict.get('publishable'))
            and (
                verified_key_decisions is not None
                or 'key_decisions' in voided_types
            )
        ),
        'required_ok': required_ok,
        'required_total': verdict.get('required_total'),
    }, public_dto.PUBLIC_BROADCAST_COMPLETENESS_FIELDS)
    canonical_public_id = meeting.get('canonical_public_id') or meeting.get('public_id') or ''
    source = {
        'success': True,
        'public_id': canonical_public_id,
        'meeting_title': meeting.get('meeting_title') or '',
        'meeting_date': meeting.get('meeting_date') or '',
        'meeting_time': meeting.get('meeting_time') or '',
        'meeting_location': meeting.get('meeting_location') or '',
        'meeting_status': meeting.get('meeting_status') or '',
        'city': meeting.get('city_name') or '',
        'county': meeting.get('county') or '',
        'state': meeting.get('state') or '',
        'agenda_url': meeting.get('agenda_url') or '',
        'minutes_url': meeting.get('minutes_url') or '',
        'agenda_packet_url': meeting.get('agenda_packet_url') or '',
        'ecomment_url': meeting.get('ecomment_url') or '',
        'video_url': (
            (work_order['youtube_video_url'] if work_order else '')
            or meeting.get('video_url') or ''
        ),
        'published_at': meeting.get('published_at') or '',
        'approved_at': (work_order['approved_at'] if work_order else '') or '',
        'completeness': completeness,
        'outputs': outputs,
    }
    return jsonify(_project_public_dto(source, public_dto.PUBLIC_BROADCAST_FIELDS))


_SIM_QUERY_SLOTS = (0, 1, 2)
_SIM_QUERY_SHARED_PROVENANCE_FIELDS = (
    'prompt_name',
    'prompt_version',
    'prompt_hash',
    'vocab_version',
    'model_id',
    'run_id',
    'generated_at',
)
_SIM_QUERY_REQUIRED_TEXT_FIELDS = (
    'question_text',
    'answer_text',
    *_SIM_QUERY_SHARED_PROVENANCE_FIELDS,
    'query_hash',
    'answer_digest',
    'retrieved_chunk_ids',
)
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')


def _validated_public_sim_query_triplet(rows) -> list[dict]:
    """Validate the private storage artifact before projecting its DTO."""
    items = [dict(row) for row in rows]
    if not items:
        return []
    if len(items) != len(_SIM_QUERY_SLOTS):
        raise ValueError(f'expected 3 sim-query rows, found {len(items)}')

    slots = []
    for item in items:
        slot = item.get('query_slot')
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise ValueError(f'invalid sim-query slot {slot!r}')
        slots.append(slot)
        for field in _SIM_QUERY_REQUIRED_TEXT_FIELDS:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'sim-query slot {slot} has invalid {field}')

        if item['prompt_name'] != 'sim_query_answer':
            raise ValueError(f'sim-query slot {slot} has unknown prompt_name')
        for field in ('prompt_hash', 'query_hash', 'answer_digest'):
            if _SHA256_RE.fullmatch(item[field]) is None:
                raise ValueError(f'sim-query slot {slot} has invalid {field}')
        expected_query_hash = hashlib.sha256(
            item['question_text'].encode('utf-8')
        ).hexdigest()
        expected_answer_digest = hashlib.sha256(
            item['answer_text'].encode('utf-8')
        ).hexdigest()
        if item['query_hash'] != expected_query_hash:
            raise ValueError(f'sim-query slot {slot} query_hash mismatch')
        if item['answer_digest'] != expected_answer_digest:
            raise ValueError(f'sim-query slot {slot} answer_digest mismatch')

        try:
            UUID(item['run_id'])
        except (ValueError, AttributeError):
            raise ValueError(f'sim-query slot {slot} has invalid run_id') from None
        generated_at = item['generated_at']
        if not generated_at.endswith('Z'):
            raise ValueError(f'sim-query slot {slot} generated_at is not UTC-Z')
        timestamp_body = generated_at[:-1]
        if 'T' not in timestamp_body:
            raise ValueError(
                f'sim-query slot {slot} has invalid generated_at'
            )
        try:
            parsed_generated_at = datetime.fromisoformat(
                timestamp_body + '+00:00'
            )
        except ValueError:
            raise ValueError(
                f'sim-query slot {slot} has invalid generated_at'
            ) from None
        if (
            parsed_generated_at.tzinfo is None
            or parsed_generated_at.utcoffset()
            != timezone.utc.utcoffset(None)
        ):
            raise ValueError(
                f'sim-query slot {slot} has invalid generated_at'
            )

        try:
            chunk_ids = json.loads(item['retrieved_chunk_ids'])
        except json.JSONDecodeError:
            raise ValueError(
                f'sim-query slot {slot} has invalid retrieved_chunk_ids'
            ) from None
        if (
            not isinstance(chunk_ids, list)
            or not chunk_ids
            or any(
                isinstance(chunk_id, bool)
                or not isinstance(chunk_id, int)
                or chunk_id < 0
                for chunk_id in chunk_ids
            )
        ):
            raise ValueError(
                f'sim-query slot {slot} has invalid retrieved_chunk_ids'
            )

    if tuple(sorted(slots)) != _SIM_QUERY_SLOTS:
        raise ValueError(f'invalid sim-query slot set {sorted(slots)!r}')
    for field in _SIM_QUERY_SHARED_PROVENANCE_FIELDS:
        if len({item[field] for item in items}) != 1:
            raise ValueError(f'sim-query generation has mixed {field}')
    return sorted(items, key=lambda item: item['query_slot'])


@bp.route('/public-api/broadcasts/<public_id>/sim-queries', methods=['GET'])
@_public_rate_limited('public_read')
def public_broadcast_sim_queries(public_id):
    resolved = _resolve_visible_public_meeting(public_id)
    if resolved is None:
        return jsonify({'success': False, 'error': 'broadcast not found'}), 404
    meeting, meeting_id = resolved

    conn = None
    try:
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT query_slot, question_text, answer_text, prompt_name,
                   prompt_version, prompt_hash, vocab_version, query_hash,
                   answer_digest, model_id, retrieved_chunk_ids, run_id,
                   generated_at
            FROM episode_sim_queries
            WHERE meeting_id = ?
            ORDER BY query_slot
            """,
            (meeting_id,),
        ).fetchall()
    except Exception:
        current_app.logger.exception(
            'sim-query storage read failed for public_id=%s', public_id
        )
        return jsonify({'error': 'sim_queries_unavailable'}), 500
    finally:
        if conn is not None:
            conn.close()

    try:
        triplet = _validated_public_sim_query_triplet(rows)
    except (TypeError, ValueError):
        current_app.logger.exception(
            'corrupt sim-query triplet for public_id=%s meeting_id=%s',
            public_id,
            meeting_id,
        )
        return jsonify({'error': 'sim_queries_corrupt'}), 500

    canonical_public_id = (
        meeting.get('canonical_public_id') or meeting.get('public_id') or ''
    )
    sim_queries = [
        _project_public_dto(
            {
                'question': item['question_text'],
                'answer': item['answer_text'],
                'generated_at': item['generated_at'],
                'model_id': item['model_id'],
            },
            public_dto.PUBLIC_SIM_QUERY_FIELDS,
        )
        for item in triplet
    ]
    return jsonify(_project_public_dto(
        {
            'public_id': canonical_public_id,
            'status': 'ready' if sim_queries else 'not_generated',
            'sim_queries': sim_queries,
        },
        public_dto.PUBLIC_SIM_QUERIES_FIELDS,
    ))


@bp.route('/public-api/broadcasts/<public_id>/sidecars/<output_type>', methods=['GET'])
@_public_rate_limited('public_read')
def public_broadcast_sidecar(public_id, output_type):
    resolved = _resolve_visible_public_meeting(public_id)
    if resolved is None or output_type not in {'quotes', 'decisions', 'routing', 'recusals'}:
        return jsonify({'success': False, 'error': 'sidecar not found'}), 404
    _meeting, meeting_id = resolved
    # Every currently-public sidecar is subordinate to the Key Decisions
    # generation in BroadcastPage. Suppress the direct sidecar doors too;
    # otherwise voiding the section would hide its prose while leaving its
    # decision excerpts, bound quotes, routing, and recusals fetchable.
    conn = get_connection()
    try:
        generation = conn.execute(
            """
            SELECT voided_at FROM notebook_outputs
            WHERE meeting_id = ? AND output_type = 'key_decisions'
            """,
            (meeting_id,),
        ).fetchone()
    finally:
        conn.close()
    if generation is not None and generation['voided_at'] is not None:
        return jsonify({'success': False, 'error': 'sidecar not found'}), 404
    from flagship_sync import _preview_root, _sidecar_path  # noqa: PLC0415
    path = _sidecar_path(_preview_root(), meeting_id, output_type)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return jsonify({'success': False, 'error': 'sidecar not found'}), 404
    except (json.JSONDecodeError, OSError):
        app.logger.exception('public sidecar read failed for %s/%s', public_id, output_type)
        return jsonify({'success': False, 'error': 'sidecar unavailable'}), 500

    if output_type == 'decisions':
        data = _materialize_decision_excerpts_for_response(meeting_id, data)
    data = _genericize_speaker_attribution(data)

    if output_type == 'quotes':
        quotes = []
        for raw in data.get('quotes') or []:
            if not isinstance(raw, dict):
                continue
            source = dict(raw)
            source['word_timings'] = _public_word_timings(raw.get('word_timings'))
            quotes.append(_project_public_dto(source, public_dto.PUBLIC_QUOTE_FIELDS))
        payload = _project_public_dto({
            'success': True, 'output_type': output_type,
            'quotes': quotes, 'quote_count': len(quotes),
        }, public_dto.PUBLIC_QUOTES_SIDECAR_FIELDS)
    elif output_type == 'decisions':
        decisions = []
        for raw in data.get('decisions') or []:
            if not isinstance(raw, dict):
                continue
            spans = []
            for span in raw.get('verbatim_spans') or []:
                if not isinstance(span, dict):
                    continue
                source = dict(span)
                source['word_timings'] = [
                    _project_public_dto(
                        timing,
                        public_dto.PUBLIC_DECISION_WORD_TIMING_FIELDS,
                    )
                    for timing in span.get('word_timings') or []
                    if isinstance(timing, dict)
                ]
                spans.append(_project_public_dto(
                    source, public_dto.PUBLIC_DECISION_SPAN_FIELDS,
                ))
            decisions.append(_project_public_dto({
                'index': raw.get('index'), 'verbatim_spans': spans,
            }, public_dto.PUBLIC_DECISION_FIELDS))
        payload = _project_public_dto({
            'success': True, 'output_type': output_type,
            'citation_modality': data.get('citation_modality'),
            'prose_output': data.get('prose_output') or '',
            'prose_list_count': data.get('prose_list_count') or len(decisions),
            'decisions': decisions,
        }, public_dto.PUBLIC_DECISIONS_SIDECAR_FIELDS)
    elif output_type == 'routing':
        routing = [
            _project_public_dto(row, public_dto.PUBLIC_ROUTING_ENTRY_FIELDS)
            for row in (data.get('routing') or []) if isinstance(row, dict)
        ]
        payload = _project_public_dto({
            'success': True, 'output_type': output_type, 'routing': routing,
        }, public_dto.PUBLIC_ROUTING_SIDECAR_FIELDS)
    else:
        recusals = []
        for raw in data.get('recusals') or []:
            if not isinstance(raw, dict):
                continue
            citation = _project_public_dto(
                raw.get('citation') if isinstance(raw.get('citation'), dict) else {},
                public_dto.PUBLIC_RECUSAL_CITATION_FIELDS,
            )
            source = dict(raw)
            source['citation'] = citation
            recusals.append(_project_public_dto(source, public_dto.PUBLIC_RECUSAL_FIELDS))
        payload = _project_public_dto({
            'success': True, 'output_type': output_type,
            'recusal_count': len(recusals), 'recusals': recusals,
        }, public_dto.PUBLIC_RECUSALS_SIDECAR_FIELDS)
    return jsonify(payload)


@bp.route('/public-api/broadcasts/<public_id>/citation', methods=['GET'])
@_public_rate_limited('citation')
def public_broadcast_citation(public_id):
    resolved = _resolve_visible_public_meeting(public_id)
    if resolved is None:
        return jsonify({'success': False, 'error': 'citation not found'}), 404
    meeting, meeting_id = resolved
    tree = _build_citation_tree(meeting_id, anonymize=True)
    if tree is None:
        return jsonify({'success': False, 'error': 'citation not found'}), 404

    meeting_dto = _project_public_dto({
        'public_id': meeting.get('canonical_public_id') or meeting.get('public_id') or '',
        **(tree.get('meeting') or {}),
    }, public_dto.PUBLIC_CITATION_MEETING_FIELDS)
    publication = _project_public_dto(
        tree.get('publication') or {}, public_dto.PUBLIC_CITATION_PUBLICATION_FIELDS,
    )
    source_tree = tree.get('sources') or {}
    primary_video = _project_public_dto(
        source_tree.get('primary_video') or {},
        public_dto.PUBLIC_CITATION_PRIMARY_VIDEO_FIELDS,
    )
    sources = _project_public_dto({
        **source_tree, 'primary_video': primary_video,
    }, public_dto.PUBLIC_CITATION_SOURCES_FIELDS)
    transcription = _project_public_dto(
        tree.get('transcription') or {},
        public_dto.PUBLIC_CITATION_TRANSCRIPTION_FIELDS,
    )
    extraction_tree = tree.get('extraction') or {}
    extraction_outputs = [
        _project_public_dto(row, public_dto.PUBLIC_CITATION_EXTRACTION_OUTPUT_FIELDS)
        for row in (extraction_tree.get('outputs') or [])
        if isinstance(row, dict)
        and row.get('output_type') in public_dto.PUBLIC_BROADCAST_OUTPUT_TYPES
    ]
    extraction = _project_public_dto({
        'pipeline': extraction_tree.get('pipeline') or '',
        'outputs': extraction_outputs,
        'output_count': len(extraction_outputs),
    }, public_dto.PUBLIC_CITATION_EXTRACTION_FIELDS)
    verification_tree = tree.get('verification') or {}
    member_quotes = _project_public_dto(
        verification_tree.get('member_quotes') or {},
        public_dto.PUBLIC_CITATION_COUNT_SUMMARY_FIELDS,
    )
    verification = _project_public_dto({
        **verification_tree, 'member_quotes': member_quotes,
    }, public_dto.PUBLIC_CITATION_VERIFICATION_FIELDS)
    corrections_tree = tree.get('corrections') or {}
    dictionary = [
        _project_public_dto(row, public_dto.PUBLIC_CITATION_DICTIONARY_ENTRY_FIELDS)
        for row in (corrections_tree.get('corrections_dictionary') or [])
        if isinstance(row, dict)
    ]
    corrections = _project_public_dto({
        **corrections_tree, 'corrections_dictionary': dictionary,
    }, public_dto.PUBLIC_CITATION_CORRECTIONS_FIELDS)
    human_review = _project_public_dto(
        tree.get('human_review') or {}, public_dto.PUBLIC_CITATION_HUMAN_REVIEW_FIELDS,
    )
    citation = _project_public_dto({
        'meeting': meeting_dto,
        'publication': publication,
        'sources': sources,
        'transcription': transcription,
        'extraction': extraction,
        'verification': verification,
        'corrections': corrections,
        'human_review': human_review,
    }, public_dto.PUBLIC_CITATION_FIELDS)
    return jsonify(_project_public_dto(
        {'success': True, 'citation': citation},
        public_dto.PUBLIC_CITATION_RESPONSE_FIELDS,
    ))


@bp.route('/public-api/cast/<city_name>', methods=['GET'])
@_public_rate_limited('public_read')
def public_cast_roster(city_name):
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT name, role, seat_id, term_started, term_ends, source_url
               FROM council_members
               WHERE city_name COLLATE NOCASE = ? AND seat_id IS NOT NULL""",
            (city_name,),
        ).fetchall()
    finally:
        conn.close()
    members = [_public_cast_member(dict(row)) for row in rows]
    members.sort(key=lambda row: (_role_sort_key(row.get('role')), row.get('seat_id') or ''))
    intel = _load_city_intelligence(city_name) or {}
    return jsonify(_project_public_dto({
        'city': city_name,
        'county': intel.get('county') or '',
        'state': intel.get('state') or '',
        'members': members,
    }, public_dto.PUBLIC_CAST_ROSTER_FIELDS))


@bp.route('/public-api/cast/<city_name>/<seat_id>', methods=['GET'])
@_public_rate_limited('public_read')
def public_cast_seat(city_name, seat_id):
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT name, role, seat_id, term_started, term_ends, source_url
               FROM council_members
               WHERE city_name COLLATE NOCASE = ? AND seat_id = ?""",
            (city_name, seat_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify({'error': 'member not found'}), 404
    intel = _load_city_intelligence(city_name) or {}
    return jsonify(_project_public_dto({
        'city': city_name,
        'county': intel.get('county') or '',
        'state': intel.get('state') or '',
        'city_official_url': intel.get('primary_source_url') or '',
        'member': _public_cast_member(dict(row)),
    }, public_dto.PUBLIC_CAST_SEAT_FIELDS))


@bp.route('/public-api/ledger/<city_name>', methods=['GET'])
@_public_rate_limited('public_heavy_read')
def public_city_ledger(city_name):
    _user, _err = _require_owner()
    if _err:
        return _err
    status_values = [
        value.strip().lower() for value in (request.args.get('status') or '').split(',')
        if value.strip()
    ]
    aged = (request.args.get('aged') or '').strip().lower() in {'1', 'true', 'yes'}
    limit = _public_int_arg('limit', 500, 1, 2000)
    conditions = ['m.city_name COLLATE NOCASE = ?']
    params: list[Any] = [city_name]
    if status_values:
        conditions.append(f"tc.status IN ({','.join('?' for _ in status_values)})")
        params.extend(status_values)
    if aged:
        conditions.extend([
            "tc.status = 'active'",
            'tc.time_horizon_months IS NOT NULL',
            "datetime(tc.extracted_at, '+' || tc.time_horizon_months || ' months') < datetime('now')",
        ])
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT m.public_id AS meeting_public_id,
                   tc.claim_type, tc.claim_text, tc.expected_outcome,
                   tc.time_horizon_months, tc.topic_tags, tc.confidence,
                   tc.context, tc.word_timings, tc.status,
                   tc.status_updated_at, tc.status_evidence,
                   cm.name AS speaker_name, cm.seat_id,
                   cm.role AS speaker_role, m.meeting_date,
                   m.meeting_title,
                   COALESCE((SELECT w.youtube_video_url FROM work_orders w
                             WHERE w.meeting_id = m.id
                               AND COALESCE(w.youtube_video_url, '') != ''
                             ORDER BY w.id DESC LIMIT 1), m.video_url, '') AS video_url,
                   m.county AS meeting_county, m.state AS meeting_state
            FROM tracked_claims tc
            JOIN council_members cm ON cm.id = tc.member_id
            JOIN meetings m ON m.id = tc.meeting_id
            WHERE {' AND '.join(conditions)}{public_serving_sql('m')}
            ORDER BY m.meeting_date DESC, tc.extracted_at DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    claims = []
    first_row = dict(rows[0]) if rows else {}
    for raw in rows:
        row = dict(raw)
        row['topic_tags'] = _public_json_list(row.get('topic_tags'))
        row['word_timings'] = _public_word_timings(row.get('word_timings'))
        row = _genericize_speaker_attribution(row)
        claims.append(_project_public_dto(row, public_dto.PUBLIC_LEDGER_CLAIM_FIELDS))
    intel = _load_city_intelligence(city_name) or {}
    return jsonify(_project_public_dto({
        'city': city_name,
        'county': intel.get('county') or first_row.get('meeting_county') or '',
        'state': intel.get('state') or first_row.get('meeting_state') or '',
        'count': len(claims),
        'tracked_claims': claims,
    }, public_dto.PUBLIC_LEDGER_FIELDS))


@bp.route('/public-api/guide', methods=['GET'])
@_public_rate_limited('public_read')
def public_guide():
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT m.public_id, ls.city_name, ls.state, ls.county,
                   ls.channel_id, ls.video_id, ls.video_url, ls.title,
                   ls.started_at
            FROM live_streams ls
            JOIN meetings m ON m.id = ls.meeting_id
            WHERE ls.is_live = 1{public_serving_sql('m')}
            ORDER BY ls.detected_at DESC
            """
        ).fetchall()
        today = datetime.now().date().isoformat()
        scheduled_today = int(conn.execute(
            f"""SELECT COUNT(DISTINCT m.city_name || '|' || m.state)
                FROM meetings m WHERE m.meeting_date = ?{public_serving_sql('m')}""",
            (today,),
        ).fetchone()[0])
    finally:
        conn.close()
    streams = [
        _project_public_dto(dict(row), public_dto.PUBLIC_GUIDE_STREAM_FIELDS)
        for row in rows
    ]
    return jsonify(_project_public_dto({
        'ok': True, 'live': streams, 'count': len(streams),
        'scheduled_today': scheduled_today,
    }, public_dto.PUBLIC_GUIDE_FIELDS))


@bp.route('/public-api/coverage', methods=['GET'])
@_public_rate_limited('public_read')
def public_coverage():
    _user, _err = _require_owner()
    if _err:
        return _err
    from pathlib import Path
    index_path = Path(__file__).resolve().parent / 'coverage_index.json'
    try:
        index = json.loads(index_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        index = {'cities': []}
    except (json.JSONDecodeError, OSError):
        app.logger.exception('public coverage index read failed')
        return jsonify({'success': False, 'error': 'coverage unavailable'}), 500
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT m.city_name, COUNT(*) AS n, MAX(m.meeting_date) AS latest
                FROM meetings m WHERE 1=1{public_serving_sql('m')}
                GROUP BY m.city_name"""
        ).fetchall()
    finally:
        conn.close()
    visible = {row['city_name']: (row['n'], row['latest']) for row in rows}
    cities = []
    for raw in index.get('cities') or []:
        if not isinstance(raw, dict):
            continue
        count, latest = visible.get(raw.get('city'), (0, None))
        status = (
            'covered' if count
            else _COVERAGE_PUBLIC_LABELS.get(raw.get('coverage'), 'assessment pending')
        )
        cities.append(_project_public_dto({
            'city': raw.get('city') or '',
            'county': raw.get('county') or '',
            'state': (raw.get('state') or '').upper(),
            'status': status,
            'published_count': int(count or 0),
            'latest_published_date': latest or '',
        }, public_dto.PUBLIC_COVERAGE_CITY_FIELDS))
    cities.sort(key=lambda row: (row['state'], row['county'], row['city']))
    return jsonify(_project_public_dto({
        'success': True,
        'status': 'ok' if cities else 'empty',
        'count': len(cities),
        'cities': cities,
    }, public_dto.PUBLIC_COVERAGE_FIELDS))


@bp.route('/public-api/corrections', methods=['GET'])
@_public_rate_limited('public_read')
def public_corrections():
    _user, _err = _require_owner()
    if _err:
        return _err
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT COALESCE(m.public_id, '') AS public_id,
                   c.corrected_surface, c.status, c.summary_public,
                   c.reported_at, c.resolved_at, m.city_name,
                   m.meeting_date, m.meeting_title
            FROM corrections c
            LEFT JOIN meetings m ON m.id = c.meeting_id
            WHERE c.meeting_id IS NULL OR (
                m.id IS NOT NULL AND m.is_published = 1
                AND EXISTS (SELECT 1 FROM work_orders w
                            WHERE w.meeting_id = m.id
                              AND w.approved_at IS NOT NULL)
            )
            ORDER BY c.reported_at DESC
            """
        ).fetchall()
    finally:
        conn.close()
    corrections = [
        _project_public_dto(dict(row), public_dto.PUBLIC_CORRECTION_FIELDS)
        for row in rows
    ]
    return jsonify(_project_public_dto({
        'success': True, 'count': len(corrections), 'corrections': corrections,
    }, public_dto.PUBLIC_CORRECTIONS_FIELDS))


@bp.route('/public-api/travelers', methods=['GET'])
@_public_rate_limited('public_read')
def public_travelers():
    return jsonify(_project_public_dto({
        'success': True, 'count': count_users(),
    }, public_dto.PUBLIC_TRAVELERS_FIELDS))


@bp.route('/public-api/youtube/embed-check', methods=['GET'])
@_public_rate_limited('public_external')
def public_youtube_embed_check():
    video_id = (request.args.get('video_id') or '').strip()
    if re.fullmatch(r'[\w-]{11}', video_id) is None:
        return jsonify({'error': 'invalid video_id'}), 400
    embeddable = _youtube_embed_cache_get(video_id)
    if embeddable is None:
        cache_ttl = _PUBLIC_YOUTUBE_EMBED_CACHE_TTL_SECONDS
        try:
            response = requests.get(
                'https://www.youtube.com/oembed',
                params={
                    'url': f'https://www.youtube.com/watch?v={video_id}',
                    'format': 'json',
                },
                timeout=6.0,
            )
            embeddable = response.status_code == 200
        except requests.RequestException:
            embeddable = True
            cache_ttl = _PUBLIC_YOUTUBE_EMBED_ERROR_CACHE_TTL_SECONDS
        _youtube_embed_cache_put(video_id, embeddable, cache_ttl)
    result = jsonify(_project_public_dto(
        {'embeddable': embeddable}, public_dto.PUBLIC_YOUTUBE_EMBED_FIELDS,
    ))
    result.headers['Cache-Control'] = 'public, max-age=900'
    return result
