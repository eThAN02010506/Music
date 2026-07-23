from __future__ import annotations

from typing import Any


def chunk_response_format() -> dict[str, Any]:
    timed_item = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "start_s": {"type": "number", "minimum": 0},
            "end_s": {"type": "number", "minimum": 0},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["text"],
        "additionalProperties": False,
    }
    lyric_item = {
        **timed_item,
        "properties": {
            **timed_item["properties"],
            "language": {"type": "string"},
        },
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "music_chunk_analysis",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "lyrics": {"type": "array", "items": lyric_item},
                    "instruments": {"type": "array", "items": {"type": "string"}},
                    "sound_events": {"type": "array", "items": timed_item},
                    "emotion_timeline": {"type": "array", "items": timed_item},
                    "themes": {"type": "array", "items": {"type": "string"}},
                    "narrative": {"type": "string"},
                },
                "required": [
                    "lyrics",
                    "instruments",
                    "sound_events",
                    "emotion_timeline",
                    "themes",
                    "narrative",
                ],
                "additionalProperties": False,
            },
        },
    }


def recovery_response_format(missing: list[str] | None = None) -> dict[str, Any]:
    chunk_schema = chunk_response_format()["json_schema"]["schema"]
    requested = [
        field
        for field in (missing or ["lyrics", "emotion_timeline"])
        if field in {"lyrics", "emotion_timeline"}
    ]
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "music_missing_fields",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    field: chunk_schema["properties"][field] for field in requested
                },
                "required": requested,
                "additionalProperties": False,
            },
        },
    }


def final_response_format() -> dict[str, Any]:
    atmosphere_item = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "basis": {"type": "string"},
        },
        "required": ["text", "confidence", "basis"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "music_final_report",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "themes": {"type": "array", "items": {"type": "string"}},
                    "narrative": {"type": "string"},
                    "inferred_atmosphere": {
                        "type": "array",
                        "items": atmosphere_item,
                    },
                },
                "required": ["themes", "narrative", "inferred_atmosphere"],
                "additionalProperties": False,
            },
        },
    }
