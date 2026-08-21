"""
Shared meeting field normalization.
Converts the diverse key formats from individual parsers into a consistent schema.

V1-Catalog-1 follow-up (2026-06-12): meeting_date is now canonicalized
to ISO YYYY-MM-DD on the way in. Per CLAUDE.md canonical schema, that's
the format the rest of the stack expects (the frontend's date helpers
assume ISO; the API's year-extraction is now format-tolerant but the
cleaner answer is to normalize at write-time). Parsers can hand us
whatever they get from the city site — "April 1, 2026", "4/1/2026",
ISO — and this layer converts.

Bugfix 2026-06-25: prior version called dateutil.parser lazily inside
a bare try/except. When dateutil was NOT installed in the running
Python env (system python3.11 doesn't have it; only .venv-worker does),
the ImportError was caught by the bare except and `_to_iso_date`
silently returned the input verbatim. Flask was running on system
python (manual launch, not via the launchd plist), so 891 non-ISO
rows accumulated in the cache before this was caught (Maricopa
parser sweep 2026-06-25). Per project rule F8 ("succeeded-empty vs
failed-silent disambiguation") this was a textbook violation: the
function returned a string (looked like success) when it had actually
failed to normalize. Rewrite uses stdlib-only logic for common
formats + falls back to dateutil ONLY when stdlib parsing didn't match,
+ logs a warning when an unknown format was encountered (loud-fail).
"""
import logging
import re
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Compile once: precise match for ISO date prefix (with or without time)
_ISO_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})(?:[T ]|$)')

# Stdlib strptime formats to try in order. Order matters: more specific
# patterns first so ambiguous strings don't get mis-parsed.
_STDLIB_FORMATS = (
    "%Y-%m-%d",              # ISO date
    "%Y-%m-%dT%H:%M:%S",     # ISO with time
    "%B %d, %Y",             # "April 1, 2026"
    "%b %d, %Y",             # "Apr 1, 2026"
    "%B %d %Y",              # "April 1 2026"  (no comma)
    "%b %d %Y",              # "Apr 1 2026"
    "%m/%d/%Y",              # "4/1/2026"  (US slash, 4-digit year)
    "%m-%d-%Y",              # "4-1-2026"  (US dash, 4-digit year)
    "%m/%d/%y",              # "4/1/26"    (US slash, 2-digit year — strptime infers century)
    "%m-%d-%y",              # "4-1-26"    (US dash, 2-digit year)
    "%d %B %Y",              # "1 April 2026" (some Granicus exports)
    "%d %b %Y",              # "1 Apr 2026"
)


def _to_iso_date(value: Any) -> Optional[str]:
    """Convert a raw meeting-date value into canonical YYYY-MM-DD.

    Strategy:
      1. None / empty / whitespace-only → None.
      2. Non-string with .date()/.isoformat() → call them.
      3. ISO prefix already → take the 10-char prefix.
      4. Try each _STDLIB_FORMATS pattern (stdlib only — works in any env).
      5. Fall back to dateutil.parser IF importable (defensive — handles
         oddballs not covered by the stdlib list).
      6. If nothing matched, log a warning naming the unknown format
         (so future drift surfaces loudly) and return None — F8
         disambiguation: return-None means "could not normalize," NOT
         "no date present" (which is also None — caller can distinguish
         by whether they passed in a non-empty string).

    Returns the ISO YYYY-MM-DD string, or None when unparseable.
    """
    if value is None:
        return None
    # Non-string with isoformat (date/datetime) — handle directly.
    if not isinstance(value, str):
        try:
            return value.date().isoformat() if hasattr(value, 'date') else value.isoformat()
        except Exception:
            return None
    # Normalize whitespace: NBSP (\xa0) + collapse multi-space → single
    # space. Cottonwood's parser emits "Apr\xa0 1, 2025" (NBSP + space)
    # which is invisible drift if not stripped.
    s = re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()
    if not s:
        return None
    # ISO already? Take the 10-char prefix.
    m = _ISO_RE.match(s)
    if m:
        try:
            datetime.strptime(m.group(0)[:10], "%Y-%m-%d")  # validates day-in-month
            return m.group(0)[:10]
        except ValueError:
            pass  # fall through — ISO-shaped but invalid (e.g. 2026-02-31)
    # Try each stdlib format in order.
    for fmt in _STDLIB_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    # Fall back to dateutil if available (defensive — covers edge cases
    # the stdlib list doesn't). Import inside the try so an ImportError
    # is caught explicitly + logged distinctly from a parse failure.
    try:
        from dateutil import parser as _du_parser  # noqa: PLC0415 — lazy
        try:
            return _du_parser.parse(s, fuzzy=False).date().isoformat()
        except Exception:
            pass
    except ImportError:
        # dateutil not in this Python env. Stdlib was tried + failed
        # above; log once and fall through to the warning-and-None return.
        pass
    # Unknown format. Loud-fail per F8 (don't return verbatim — that
    # masks a real bug as silent success). Logging gives operator a
    # grep target for any new format that appears in the wild.
    logger.warning(
        "normalize._to_iso_date: could not parse date string %r "
        "(stdlib formats + dateutil exhausted) — returning None. "
        "Add the format to _STDLIB_FORMATS if it's a legitimate variant.",
        value,
    )
    return None


FIELD_MAPPINGS = {
    'Meeting Title/Name': 'meeting_title', 'meeting_title': 'meeting_title',
    'title': 'meeting_title', 'name': 'meeting_title', 'event_title': 'meeting_title',
    'event_name': 'meeting_title', 'summary': 'meeting_title', 'subject': 'meeting_title',

    'Meeting Date': 'meeting_date', 'meeting_date': 'meeting_date',
    'date': 'meeting_date', 'event_date': 'meeting_date', 'start_date': 'meeting_date',
    'scheduled_date': 'meeting_date', 'published': 'meeting_date',
    'pubDate': 'meeting_date', 'pub_date': 'meeting_date',

    'Meeting Time': 'meeting_time', 'meeting_time': 'meeting_time',
    'time': 'meeting_time', 'start_time': 'meeting_time', 'scheduled_time': 'meeting_time',

    'Meeting Status': 'meeting_status', 'meeting_status': 'meeting_status',
    'status': 'meeting_status', 'event_status': 'meeting_status',

    'Agenda URL': 'agenda_url', 'agenda_url': 'agenda_url',
    'agenda': 'agenda_url', 'agenda_link': 'agenda_url',
    'link': 'agenda_url', 'url': 'agenda_url',

    'Minutes URL': 'minutes_url', 'minutes_url': 'minutes_url',
    'minutes': 'minutes_url', 'minutes_link': 'minutes_url',

    'Video URL': 'video_url', 'video_url': 'video_url',
    'video': 'video_url', 'video_link': 'video_url', 'stream_url': 'video_url',

    'Agenda Packet URL': 'agenda_packet_url', 'agenda_packet_url': 'agenda_packet_url',
    'packet_url': 'agenda_packet_url',

    'eComment/Public Comment URL': 'ecomment_url', 'ecomment_url': 'ecomment_url',
    'comment_url': 'ecomment_url',

    'Meeting Location': 'meeting_location', 'meeting_location': 'meeting_location',
    'location': 'meeting_location', 'venue': 'meeting_location', 'place': 'meeting_location',

    'Meeting ID': 'meeting_id', 'meeting_id': 'meeting_id',
    'event_id': 'meeting_id', 'id': 'meeting_id',
}


def normalize_meeting_fields(meeting: dict) -> dict:
    """Normalize meeting field names to a consistent schema."""
    normalized = {}
    for key, value in meeting.items():
        normalized_key = FIELD_MAPPINGS.get(key, key.lower().replace(' ', '_').replace('/', '_'))
        normalized[normalized_key] = value

    if 'meeting_title' not in normalized:
        normalized['meeting_title'] = normalized.get('title', 'Untitled Meeting')

    if 'meeting_location' not in normalized and 'location' in normalized:
        normalized['meeting_location'] = normalized['location']

    # V1-Catalog-1 follow-up: canonical ISO date.
    if 'meeting_date' in normalized:
        normalized['meeting_date'] = _to_iso_date(normalized['meeting_date'])

    return normalized
