"""Validation and plain-text rendering for meeting minutes."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any


SECTIONS = ("decisions", "owners", "deadlines", "next", "open")
DISPLAY_SECTIONS = ("decisions", "owners", "deadlines", "next")
MAX_ITEMS = 200
MAX_TOTAL_CHARS = 20_000
MAX_TOPIC_CHARS = 120
MAX_TEXT_CHARS = 2_000

DEFAULT_PROFILE: dict[str, Any] = {
    "labels": {
        "decisions": "決定事項",
        "owners": "担当",
        "deadlines": "期限",
        "next": "次回",
        "open": "未決事項",
    },
    "order": ["decisions", "owners", "deadlines", "next"],
    "bullet": "・",
    "section_brackets": ["【", "】"],
    "topic_brackets": ["＜", "＞"],
    "style_instructions": "",
}


def _checked_string(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not value.strip():
        raise ValueError(f"{field} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    # Reject newlines and control/format characters rather than allowing content
    # to escape its line or inject terminal/rendering controls.
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for ch in value):
        raise ValueError(f"{field} contains a control character")
    return value.strip()


def validate_minutes(payload: object) -> dict[str, list[dict[str, str]]]:
    if not isinstance(payload, dict) or set(payload) != set(SECTIONS):
        raise ValueError("payload must contain exactly the five required sections")
    clean: dict[str, list[dict[str, str]]] = {}
    total_chars = 0
    total_items = 0
    for section in SECTIONS:
        items = payload[section]
        if not isinstance(items, list):
            raise ValueError(f"{section} must be a list")
        clean_items = []
        for index, item in enumerate(items):
            if not isinstance(item, dict) or set(item) != {"topic", "text"}:
                raise ValueError(f"{section}[{index}] must contain exactly topic and text")
            topic = _checked_string(item["topic"], f"{section}[{index}].topic", MAX_TOPIC_CHARS)
            text = _checked_string(item["text"], f"{section}[{index}].text", MAX_TEXT_CHARS)
            clean_items.append({"topic": topic, "text": text})
            total_chars += len(topic) + len(text)
            total_items += 1
        clean[section] = clean_items
    if total_items > MAX_ITEMS:
        raise ValueError(f"too many items (maximum {MAX_ITEMS})")
    if total_chars > MAX_TOTAL_CHARS:
        raise ValueError(f"content exceeds {MAX_TOTAL_CHARS} characters")
    return clean


def _profile(profile: dict | None) -> dict[str, Any]:
    result = {**DEFAULT_PROFILE, "labels": dict(DEFAULT_PROFILE["labels"])}
    if profile is not None:
        if not isinstance(profile, dict):
            raise ValueError("profile must be an object")
        unknown = set(profile) - set(DEFAULT_PROFILE)
        if unknown:
            raise ValueError(f"unknown profile fields: {', '.join(sorted(unknown))}")
        if "labels" in profile:
            labels = profile["labels"]
            if not isinstance(labels, dict) or set(labels) - set(SECTIONS):
                raise ValueError("labels must map known sections to strings")
            result["labels"].update(labels)
        if "order" in profile:
            order = profile["order"]
            if (not isinstance(order, list) or len(order) != len(DISPLAY_SECTIONS)
                    or any(not isinstance(entry, str) for entry in order)
                    or set(order) != set(DISPLAY_SECTIONS)):
                raise ValueError("order must contain each displayed section exactly once")
            result["order"] = order[:]
        if "bullet" in profile:
            result["bullet"] = profile["bullet"]
        if "section_brackets" in profile:
            result["section_brackets"] = profile["section_brackets"]
        if "topic_brackets" in profile:
            result["topic_brackets"] = profile["topic_brackets"]
        if "style_instructions" in profile:
            result["style_instructions"] = profile["style_instructions"]
    for section, label in result["labels"].items():
        result["labels"][section] = _checked_string(label, f"labels.{section}", 80)
    for key in ("bullet",):
        result[key] = _checked_string(result[key], key, 8)
    for key in ("section_brackets", "topic_brackets"):
        pair = result[key]
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError(f"{key} must be a pair of strings")
        result[key] = [_checked_string(part, key, 8) for part in pair]
    style = result["style_instructions"]
    if (not isinstance(style, str) or len(style) > 2_000
            or any(ch not in "\n\t" and unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for ch in style)):
        raise ValueError("style_instructions must be safe text of at most 2000 characters")
    return result


def load_profile(path: Path | None) -> dict[str, Any]:
    if path is None:
        return _profile(None)
    try:
        if not path.is_file():
            raise ValueError("not a regular file")
        with path.open("rb") as source:
            raw = source.read(16_001)
        if len(raw) > 16_000:
            raise ValueError("profile exceeds 16000 bytes")
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        raise ValueError("書式設定のJSONを読み込めません。UTF-8・16KB以内のファイルを選んでください。") from None
    return _profile(data)


def render(payload: object, title: str, profile: dict | None = None) -> tuple[str, str]:
    data = validate_minutes(payload)
    title = _checked_string(title, "title", 200)
    p = _profile(profile)

    # Identical bullets across categories belong to the most actionable section;
    # the next-meeting section has priority. No semantic/fuzzy deduplication.
    seen: set[tuple[str, str]] = set()
    for section in ("next", "decisions", "owners", "deadlines", "open"):
        unique = []
        for item in data[section]:
            key = (item["topic"], item["text"])
            if key not in seen:
                seen.add(key)
                unique.append(item)
        data[section] = unique

    def format_sections(sections: tuple[str, ...] | list[str]) -> str:
        lines = [title]
        sb, se = p["section_brackets"]
        tb, te = p["topic_brackets"]
        for section in sections:
            groups: dict[str, list[str]] = {}
            for item in data[section]:
                groups.setdefault(item["topic"], []).append(item["text"])
            if not groups:
                continue
            lines.append("")
            lines.append(f"{sb}{p['labels'][section]}{se}")
            for topic, texts in groups.items():
                lines.append(f"{tb}{topic}{te}")
                lines.extend(f"{p['bullet']} {text}" for text in texts)
        return "\n".join(lines)

    return format_sections(p["order"]), format_sections(["open"])
