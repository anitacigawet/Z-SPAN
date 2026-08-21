"""D-180 public-surface field allowlists.

This module is the single source of truth for the public boundary tests and
the pass-2 Flask routes.  Public DTOs are constructed from these tuples; they
are never produced by subtracting fields from an owner payload.
"""

PUBLIC_CHANNELS_TREE_FIELDS = ("ok", "states")
PUBLIC_CHANNEL_STATE_FIELDS = (
    "state", "statewide_sources", "regional_sources", "counties",
)
PUBLIC_CHANNEL_COUNTY_FIELDS = ("county", "sources", "cities")
PUBLIC_CHANNEL_CITY_FIELDS = (
    "source_id",
    "name",
    "place_type",
    "route_name",
    "source_status",
    "contribution_url",
    "meeting_count",
    "broadcast_count",
    "status",
    "last_meeting",
    "first_meeting",
    "lat",
    "lng",
)

PUBLIC_CITY_YEARS_FIELDS = ("ok", "city", "years", "current_year")
PUBLIC_CITY_MEETINGS_FIELDS = ("success", "city", "year", "count", "events")
PUBLIC_EPISODE_CARD_FIELDS = (
    "public_id",
    "city_name",
    "county",
    "state",
    "meeting_title",
    "meeting_date",
    "meeting_time",
    "meeting_location",
    "meeting_status",
    "agenda_url",
    "minutes_url",
    "agenda_packet_url",
    "video_url",
    "ecomment_url",
    "published_at",
    "availability",
    "episode_tagline",
)

PUBLIC_COUNTY_MEETINGS_FIELDS = (
    "success",
    "county",
    "total_meetings",
    "cities",
)
PUBLIC_COUNTY_MEETING_FIELDS = (
    "public_id",
    "city",
    "county",
    "meeting_title",
    "meeting_date",
    "meeting_time",
    "meeting_location",
    "meeting_status",
    "agenda_url",
    "minutes_url",
    "video_url",
)

PUBLIC_SEARCH_FIELDS = ("success", "results", "total", "limit", "offset", "has_more")
PUBLIC_SEARCH_RESULT_FIELDS = (
    "public_id",
    "city",
    "county",
    "state",
    "meeting_title",
    "meeting_date",
    "meeting_time",
    "meeting_location",
    "meeting_status",
    "agenda_url",
    "minutes_url",
    "video_url",
)

PUBLIC_CALENDAR_STATS_FIELDS = (
    "total_cities",
    "total_meetings",
    "states",
    "counties",
    "meetings_by_county",
    "top_cities",
)
PUBLIC_CALENDAR_TOP_CITY_FIELDS = ("city", "county", "meetings")
PUBLIC_HEALTH_FIELDS = ("status",)

PUBLIC_BROADCAST_FIELDS = (
    "success",
    "public_id",
    "meeting_title",
    "meeting_date",
    "meeting_time",
    "meeting_location",
    "meeting_status",
    "city",
    "county",
    "state",
    "agenda_url",
    "minutes_url",
    "agenda_packet_url",
    "ecomment_url",
    "video_url",
    "published_at",
    "approved_at",
    "completeness",
    "outputs",
)
PUBLIC_BROADCAST_COMPLETENESS_FIELDS = (
    "complete",
    "required_ok",
    "required_total",
)
PUBLIC_BROADCAST_OUTPUT_TYPES = (
    "synopsis",
    "key_decisions",
    "community_calls_to_action",
    "episode_tagline",
)
PUBLIC_BROADCAST_OUTPUT_FIELDS = ("content", "karaoke_word_timings")

PUBLIC_SIM_QUERIES_FIELDS = ("public_id", "status", "sim_queries")
PUBLIC_SIM_QUERY_FIELDS = ("question", "answer", "generated_at", "model_id")

PUBLIC_QUOTES_SIDECAR_FIELDS = ("success", "output_type", "quotes", "quote_count")
PUBLIC_QUOTE_FIELDS = (
    "speaker_name",
    "speaker_role",
    "speaker_class",
    "quote_text",
    "topic_tags",
    "selection_rationale",
    "video_timestamp_seconds",
    "word_timings",
)
PUBLIC_WORD_TIMING_FIELDS = ("word", "start_ms", "end_ms", "start", "end")

PUBLIC_DECISIONS_SIDECAR_FIELDS = (
    "success",
    "output_type",
    "citation_modality",
    "prose_output",
    "prose_list_count",
    "decisions",
)
PUBLIC_DECISION_FIELDS = ("index", "verbatim_spans")
PUBLIC_DECISION_WORD_TIMING_FIELDS = ("word", "start", "end")
PUBLIC_DECISION_SPAN_FIELDS = (
    "text",
    "char_start",
    "char_end",
    "start_seconds",
    "end_seconds",
    "source",
    "label",
    "structure",
    "omission_marker",
    "word_timings",
)

PUBLIC_ROUTING_SIDECAR_FIELDS = ("success", "output_type", "routing")
PUBLIC_ROUTING_ENTRY_FIELDS = ("quote_index", "bucket", "decision_index")

PUBLIC_RECUSALS_SIDECAR_FIELDS = (
    "success",
    "output_type",
    "recusal_count",
    "recusals",
)
PUBLIC_RECUSAL_FIELDS = (
    "speaker_name",
    "speaker_role",
    "rationale",
    "matter",
    "raw_text",
    "citation",
)
PUBLIC_RECUSAL_CITATION_FIELDS = (
    "source",
    "decision_index",
    "video_timestamp_seconds",
)

PUBLIC_CITATION_RESPONSE_FIELDS = ("success", "citation")
PUBLIC_CITATION_FIELDS = (
    "meeting",
    "publication",
    "sources",
    "transcription",
    "extraction",
    "verification",
    "corrections",
    "human_review",
)
PUBLIC_CITATION_MEETING_FIELDS = (
    "public_id",
    "city",
    "county",
    "state",
    "title",
    "date",
    "time",
    "location",
)
PUBLIC_CITATION_PUBLICATION_FIELDS = ("is_published", "published_at")
PUBLIC_CITATION_SOURCES_FIELDS = (
    "primary_video",
    "agenda_url",
    "agenda_packet_url",
    "minutes_url",
    "ecomment_url",
)
PUBLIC_CITATION_PRIMARY_VIDEO_FIELDS = ("url", "platform")
PUBLIC_CITATION_TRANSCRIPTION_FIELDS = (
    "method",
    "generated_at",
    "word_count",
    "duration_seconds",
    "primed_with_city_vocabulary",
)
PUBLIC_CITATION_EXTRACTION_FIELDS = ("pipeline", "outputs", "output_count")
PUBLIC_CITATION_EXTRACTION_OUTPUT_FIELDS = (
    "output_type",
    "generated_at",
    "has_content",
)
PUBLIC_CITATION_VERIFICATION_FIELDS = (
    "method",
    "member_quotes",
    "auto_corrections_applied",
    "per_quote_human_verifications",
)
PUBLIC_CITATION_COUNT_SUMMARY_FIELDS = ("total", "by_status")
PUBLIC_CITATION_CORRECTIONS_FIELDS = (
    "city_vocabulary_dictionary_size",
    "corrections_dictionary",
)
PUBLIC_CITATION_DICTIONARY_ENTRY_FIELDS = ("wrong", "right")
PUBLIC_CITATION_HUMAN_REVIEW_FIELDS = ("approved_at",)

PUBLIC_CAST_ROSTER_FIELDS = ("city", "county", "state", "members")
# Cast surface — sealed under the same aggregation rule as the parser
# registry (D-153, operator decision 2026-07-24). The enumerated fields
# below are the exact civic-accountability data allowed on the public
# surface: name, role, term dates, and the source URL that supports them.
# Contact fields — email, phone, address, staff numbers, anything a
# scraper could aggregate into a contact-harvesting database — are
# NEVER added to this allowlist. The scrape+aggregate threat model is
# what D-153 protects against pointed at people instead of endpoints.
# `_project_public_dto` enforces this allowlist by only copying named
# fields from source; test_cast_member_projection_drops_contact_pii in
# parsers/input_security/test_public_api_boundary.py locks the seal at
# the DTO layer even if a future SELECT starts fetching PII columns.
PUBLIC_CAST_MEMBER_FIELDS = (
    "seat_id",
    "name",
    "role",
    "term_started",
    "term_ends",
    "source_url",
)
PUBLIC_CAST_SEAT_FIELDS = (
    "city",
    "county",
    "state",
    "city_official_url",
    "member",
)

PUBLIC_LEDGER_FIELDS = ("city", "county", "state", "count", "tracked_claims")
PUBLIC_LEDGER_CLAIM_FIELDS = (
    "meeting_public_id",
    "claim_type",
    "claim_text",
    "expected_outcome",
    "time_horizon_months",
    "topic_tags",
    "confidence",
    "context",
    "word_timings",
    "status",
    "status_updated_at",
    "status_evidence",
    "speaker_name",
    "seat_id",
    "speaker_role",
    "meeting_date",
    "meeting_title",
    "video_url",
)

PUBLIC_GUIDE_FIELDS = ("ok", "live", "count", "scheduled_today")
PUBLIC_GUIDE_STREAM_FIELDS = (
    "public_id",
    "city_name",
    "state",
    "county",
    "channel_id",
    "video_id",
    "video_url",
    "title",
    "started_at",
)

PUBLIC_COVERAGE_FIELDS = ("success", "status", "count", "cities")
PUBLIC_COVERAGE_CITY_FIELDS = (
    "city",
    "county",
    "state",
    "status",
    "published_count",
    "latest_published_date",
)

PUBLIC_CORRECTIONS_FIELDS = ("success", "count", "corrections")
PUBLIC_CORRECTION_FIELDS = (
    "public_id",
    "corrected_surface",
    "status",
    "summary_public",
    "reported_at",
    "resolved_at",
    "city_name",
    "meeting_date",
    "meeting_title",
)

PUBLIC_TRAVELERS_FIELDS = ("success", "count")
PUBLIC_YOUTUBE_EMBED_FIELDS = ("embeddable",)

PUBLIC_V1_JURISDICTIONS_FIELDS = ("states",)
PUBLIC_V1_STATE_FIELDS = ("state", "counties")
PUBLIC_V1_COUNTY_FIELDS = ("county", "cities")
PUBLIC_V1_CITY_FIELDS = ("city", "meeting_count", "covered")
PUBLIC_V1_CATALOG_LIST_RESPONSE_FIELDS = ("meetings", "next_cursor")
PUBLIC_V1_CATALOG_LIST_FIELDS = (
    "public_id",
    "state",
    "county",
    "city",
    "title",
    "date",
    "time",
    "location",
    "meeting_status",
    "availability",
)
PUBLIC_V1_CATALOG_DETAIL_FIELDS = (
    *PUBLIC_V1_CATALOG_LIST_FIELDS,
    "video_url",
    "documents",
    "local_processing",
)
PUBLIC_V1_DOCUMENT_FIELDS = ("agenda_url", "minutes_url", "packet_url")
PUBLIC_V1_LOCAL_PROCESSING_FIELDS = ("status", "source_kind")
