from __future__ import annotations

from typing import Any


_SOURCE_ID = {
    "type": "string",
    "pattern": r"^[A-Za-z0-9_.:-]{1,160}$",
}
_IDENTIFIER = {
    "type": "string",
    "pattern": r"^[A-Za-z0-9_.:-]{1,80}$",
}
_CONFIDENCE = {
    "type": "number",
    "minimum": 0,
    "maximum": 1,
}
_DIMENSIONS = [
    "melody",
    "harmony",
    "rhythm",
    "timbre",
    "dynamics",
    "instrumentation",
    "space",
    "lyrics",
    "structure",
    "other",
]
_CLAIM_TYPES = [
    "observed_fact",
    "computed_fact",
    "grounded_interpretation",
    "possible_reading",
]


def _object(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or list(properties),
        "additionalProperties": False,
    }


def _bounded_string(max_length: int) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": max_length}


def _string_array(
    *,
    max_items: int,
    item_max_length: int,
    min_items: int = 0,
) -> dict[str, Any]:
    return {
        "type": "array",
        "items": _bounded_string(item_max_length),
        "minItems": min_items,
        "maxItems": max_items,
    }


def _span_properties() -> dict[str, Any]:
    return {
        "start_s": {"type": "number", "minimum": 0},
        "end_s": {"type": "number", "exclusiveMinimum": 0},
    }


def understanding_map_response_format() -> dict[str, Any]:
    """Return a compact wire schema whose source IDs are expanded locally."""

    emotional_arc_item = _object(
        {
            **_span_properties(),
            "description": _bounded_string(600),
            "evidence_source_ids": {
                "type": "array",
                "items": _SOURCE_ID,
                "minItems": 1,
                "maxItems": 8,
            },
            "confidence": _CONFIDENCE,
        }
    )
    section_item = _object(
        {
            "id": _IDENTIFIER,
            "label": _bounded_string(80),
            **_span_properties(),
            "expressive_role": _bounded_string(800),
            "confidence": _CONFIDENCE,
            "alternative_labels": _string_array(
                max_items=3,
                item_max_length=80,
            ),
        }
    )
    event_item = _object(
        {
            "id": _IDENTIFIER,
            **_span_properties(),
            "section": _bounded_string(80),
            "observation": _bounded_string(1200),
            "interpretation": _bounded_string(1200),
            "expressive_role": _bounded_string(1200),
            "evidence_source_ids": {
                "type": "array",
                "items": _SOURCE_ID,
                "minItems": 1,
                "maxItems": 10,
            },
            "lyrics_source_ids": {
                "type": "array",
                "items": {
                    "type": "string",
                    "pattern": r"^lyrics:\d+$",
                },
                "maxItems": 8,
            },
            "listening_task": _bounded_string(800),
            "alternative_readings": _string_array(
                max_items=4,
                item_max_length=600,
            ),
            "confidence": _CONFIDENCE,
        }
    )
    key_moment_item = _object(
        {
            "id": _IDENTIFIER,
            "event_id": _IDENTIFIER,
            **_span_properties(),
            "reason": _bounded_string(800),
            "listening_task": _bounded_string(800),
            "confidence": _CONFIDENCE,
        }
    )
    schema = _object(
        {
            "core_expression": _bounded_string(1000),
            "overall_atmosphere": _bounded_string(1600),
            "emotional_arc": {
                "type": "array",
                "items": emotional_arc_item,
                "maxItems": 10,
            },
            "sections": {
                "type": "array",
                "items": section_item,
                "maxItems": 16,
            },
            "events": {
                "type": "array",
                "items": event_item,
                "minItems": 1,
                "maxItems": 24,
            },
            "key_moments": {
                "type": "array",
                "items": key_moment_item,
                "maxItems": 5,
            },
            "confidence": _CONFIDENCE,
            "warnings": _string_array(max_items=5, item_max_length=600),
        }
    )
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "music_understanding_map",
            "strict": True,
            "schema": schema,
        },
    }


def teaching_chat_response_format() -> dict[str, Any]:
    # The model selects semantic content and immutable source IDs. Clickable
    # ranges, evidence cards, and player commands are expanded locally from
    # those IDs; asking small local models to reproduce that UI envelope made
    # responses slow and structurally brittle.
    schema = _object(
        {
            "answer": _bounded_string(2400),
            "source_ids": {
                "type": "array",
                "items": _SOURCE_ID,
                "maxItems": 6,
            },
            "suggested_questions": _string_array(
                max_items=4,
                item_max_length=300,
            ),
            "alternative_readings": _string_array(
                max_items=4,
                item_max_length=300,
            ),
            "confidence": _CONFIDENCE,
            "insufficient_evidence": {"type": "boolean"},
        }
    )
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "music_teaching_answer",
            "strict": True,
            "schema": schema,
        },
    }


def relisten_response_format(*, excerpt_count: int) -> dict[str, Any]:
    if excerpt_count not in {1, 2}:
        raise ValueError("relisten requires one or two excerpts")
    evidence = _object(
        {
            "range_index": {
                "type": "integer",
                "minimum": 0,
                "maximum": excerpt_count - 1,
            },
            "dimension": {"type": "string", "enum": _DIMENSIONS},
            "observation": _bounded_string(1000),
            "confidence": _CONFIDENCE,
        }
    )
    schema = _object(
        {
            "evidence": {
                "type": "array",
                "items": evidence,
                "maxItems": 12,
            },
            "warnings": _string_array(max_items=4, item_max_length=600),
        }
    )
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "music_excerpt_observations",
            "strict": True,
            "schema": schema,
        },
    }
