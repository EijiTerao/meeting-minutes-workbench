"""Bounded, explicitly routed text workflow for meeting minutes."""

from __future__ import annotations

import difflib
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable

from .core import render, validate_minutes
from .engine import SYSTEM, ollama_json
from .routes import Settings, Stage, anthropic_json, openai_json

MAX_TRANSCRIPT = 192_000
MAX_CONTEXT = 6_000
MAX_CONFIRMED = 4_000
CHUNK_BYTES = 14_000
SOURCE_BYTES = 768
SECTION_KEYS = ("decisions", "owners", "deadlines", "next", "open")
ITEM_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["topic", "text", "evidence"],
               "properties": {"topic": {"type": "string"}, "text": {"type": "string"},
                              "evidence": {"type": "array", "items": {"type": "string"}}}}
MINUTES_SCHEMA = {"type": "object", "additionalProperties": False, "required": list(SECTION_KEYS),
                  "properties": {k: {"type": "array", "items": ITEM_SCHEMA} for k in SECTION_KEYS}}
CORRECTION_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["chunks"],
                     "properties": {"chunks": {"type": "array", "items": {
                         "type": "object", "additionalProperties": False, "required": ["id", "text"],
                         "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}}}}
VERIFY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["results"],
                 "properties": {"results": {"type": "array", "items": {
                     "type": "object", "additionalProperties": False, "required": ["index", "supported"],
                     "properties": {"index": {"type": "integer"}, "supported": {"type": "boolean"}}}}}}
SELECT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["selected"],
                 "properties": {"selected": {"type": "array", "items": {"type": "integer"}}}}


@dataclass(frozen=True)
class RunResult:
    minutes: dict
    corrected: str
    chunks: int
    seconds: dict[str, float]
    corrections_rejected: int = 0
    verification_rejected: int = 0
    questions: tuple[str, ...] = ()


def _split(text: str, limit: int = SOURCE_BYTES) -> list[str]:
    """Split without dropping bytes; use natural boundaries, hard-splitting only oversized units."""
    units = re.split(r"(?<=\n)|(?<=[。！？!?])", text)
    out: list[str] = []
    buf = ""
    for unit in units:
        # A single enormous line/sentence is cut at Unicode character boundaries.
        pieces: list[str] = []
        while len(unit.encode("utf-8")) > limit:
            take = 1
            while len(unit[:take].encode("utf-8")) <= limit:
                take += 1
            take -= 1
            pieces.append(unit[:take]); unit = unit[take:]
        pieces.append(unit)
        for piece in pieces:
            if buf and len((buf + piece).encode("utf-8")) > limit:
                out.append(buf); buf = ""
            buf += piece
    if buf or not out:
        out.append(buf)
    assert "".join(out) == text
    return out


def _groups(source: dict[str, str], byte_limit: int) -> list[list[str]]:
    groups, group, size = [], [], 1_000
    for key, text in source.items():
        cost = len(text.encode("utf-8")) + len(key) + 32
        if group and size + cost > byte_limit:
            groups.append(group); group, size = [], 1_000
        group.append(key); size += cost
    if group:
        groups.append(group)
    return groups


def _call(stage: Stage, system: str, data: dict, schema: dict, authorized: bool, max_output: int,
          allow_anthropic: bool = False) -> dict:
    if stage.provider == "off":
        raise ValueError("この処理段階は無効です。")
    if stage.provider == "ollama":
        return ollama_json(stage.model, system, data, schema, max_output=max_output)
    if stage.provider == "openai":
        return openai_json(stage.model, system, data, schema, authorized=authorized, max_output=max_output)
    if stage.provider == "anthropic":
        return anthropic_json(stage.model, system, data, schema, authorized=allow_anthropic, max_output=max_output)
    raise ValueError("処理先を確認してください。")


def _protected(text: str) -> list[str]:
    return re.findall(
        r"[+-]?\d[\d,]*(?:\.\d+)?%?|(?:ではない|なかった|ません|ない|せず|しない|不可|禁止|未定|未決|"
        r"見送|撤回|中止|延期|保留|却下|否決|取りやめ|不採用|未承認|請求|支払|受け取|入金|"
        r"払戻|返金|発注|受注|not|never|no)|"
        r"[「『\"'“”‘’].+?[」』\"'“”‘’]",
        text, re.I,
    )


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"[+-]?\d[\d,]*(?:\.\d+)?", unicodedata.normalize("NFKC", text).replace(",", "")))


_REJECTED = r"承認せず|採用せず|採用しない|不採用|不承認|見送|取りやめ|却下|否決|中止|撤回|認めない"


def _rejected_number(claim: str, source: str) -> bool:
    """Block straightforward rejected proposals; this is not semantic verification."""
    if re.search(_REJECTED, claim):
        return False
    source = re.sub(r"(?<=\d),(?=\d)", "", unicodedata.normalize("NFKC", source))
    return any(
        re.search(rf"(?<![\d.]){re.escape(number)}(?![\d.])[^。\n、]{{0,24}}(?:{_REJECTED})", source)
        for number in _numbers(claim)
    )


def _safe_correction(before: str, after: str) -> bool:
    if not after.strip() or after == before or _protected(before) != _protected(after):
        return False
    ratio = difflib.SequenceMatcher(a=before, b=after, autojunk=False).ratio()
    return ratio >= 0.82 and 0.8 <= len(after) / max(1, len(before)) <= 1.25


def run_workflow(transcript: str, title: str, settings: Settings, profile: dict,
                 references: tuple[str, ...] = (), confirmed: str = "", *, authorized: bool = False,
                 allow_anthropic: bool = False, high_quality: bool = False,
                 is_cancelled: Callable[[], bool] | None = None) -> RunResult:
    if not isinstance(transcript, str) or not transcript.strip():
        raise ValueError("文字起こしを入力してください。")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("会議名を入力してください。")
    if len(transcript.encode("utf-8")) > MAX_TRANSCRIPT:
        raise ValueError("文字起こしは192KB以内にしてください。")
    if not isinstance(confirmed, str) or len(confirmed.encode("utf-8")) > MAX_CONFIRMED:
        raise ValueError("確認済み情報は4KB以内にしてください。")
    if not isinstance(references, tuple) or len(references) > 3:
        raise ValueError("参考資料は3件までです。")
    if any(not isinstance(x, str) for x in references):
        raise ValueError("参考資料の形式を確認してください。")
    context_bytes = sum(len(x.encode("utf-8")) for x in (title, profile.get("style_instructions", ""), *references))
    if context_bytes > MAX_CONTEXT:
        raise ValueError("タイトル・書式・参考資料は合計6KB以内にしてください。")
    stages = (settings.correction, settings.generation, settings.verification)
    if settings.generation.provider == "off":
        raise ValueError("議事録生成が無効です。")
    if any(s.provider == "openai" for s in stages) and authorized is not True:
        raise ValueError("オンライン処理の明示的な許可が必要です。")
    if any(s.provider == "anthropic" for s in stages) and allow_anthropic is not True:
        raise ValueError("Anthropicへの送信には今回の実行で明示的な許可が必要です。")
    if type(high_quality) is not bool:
        raise ValueError("high_quality must be a boolean")

    def check_cancelled() -> None:
        if is_cancelled is not None and is_cancelled():
            raise RuntimeError("処理を中止しました。")

    def call(stage: Stage, system: str, data: dict, schema: dict, max_output: int) -> dict:
        args = (stage, system, data, schema, authorized, max_output)
        return _call(*args, allow_anthropic=True) if allow_anthropic else _call(*args)

    parts = _split(transcript)
    source = {f"S{i+1}": part for i, part in enumerate(parts)}
    corrected_parts = dict(source)
    seconds = {"correction": 0.0, "generation": 0.0, "verification": 0.0}
    rejected = 0
    if settings.correction.provider != "off":
        for group in _groups(source, 7_000):
            check_cancelled()
            chunk_rows = [{"id": key, "text": source[key]} for key in group]
            start = time.monotonic()
            result = call(settings.correction,
                "発話の誤認識だけを最小限修正する。要約・省略・言い換えは禁止。各IDを順序通り一度ずつ保つ。",
                {"chunks": chunk_rows}, CORRECTION_SCHEMA, 8_192)
            seconds["correction"] += time.monotonic() - start
            rows = result.get("chunks")
            valid = (isinstance(rows, list) and len(rows) == len(group)
                     and all(isinstance(r, dict) and set(r) == {"id", "text"}
                             and isinstance(r.get("id"), str) and isinstance(r.get("text"), str)
                             for r in rows)
                     and [r["id"] for r in rows] == group)
            if not valid:
                rejected += len(group); continue
            for key, row in zip(group, rows):
                candidate = row["text"]
                if candidate == source[key]:
                    continue
                if len(candidate.encode("utf-8")) > SOURCE_BYTES or not _safe_correction(source[key], candidate):
                    rejected += 1
                else:
                    corrected_parts[key] = candidate
    corrected = "".join(corrected_parts[k] for k in source)

    all_items: dict[str, list[dict]] = {key: [] for key in SECTION_KEYS}
    questions: list[str] = []
    generation_schema = MINUTES_SCHEMA
    if high_quality:
        generation_schema = json.loads(json.dumps(MINUTES_SCHEMA))
        generation_schema["required"].append("questions")
        generation_schema["properties"]["questions"] = {"type": "array", "items": {"type": "string"}}
    for group in _groups(corrected_parts, CHUNK_BYTES):
        check_cancelled()
        start = time.monotonic()
        result = call(settings.generation, SYSTEM + "\n根拠は当該チャンク内のIDだけを配列で示す。提案を決定にせず、途中経過を推測せず、終盤の発言・結論を落とさない。" +
            (" 質問は、参加者、話者、日付、担当者など、確認すると今回の議事録が変わる事実だけに絞る。"
             "発言に候補がある場合は『〈具体的な候補〉で合っていますか？』と書き、候補がない場合は詳細を聞く。候補を捏造しない。"
             "実際の未決事項や確定済みの事実を質問にしない。" if high_quality else ""),
            {"title": title, "sources": [{"id": key, "text": corrected_parts[key]} for key in group],
             "confirmed": confirmed, "confirmed_id": "C1" if confirmed else "",
             "references": references, "style": profile.get("style_instructions", "")},
            generation_schema, 4_096)
        seconds["generation"] += time.monotonic() - start
        if high_quality:
            rows = result.get("questions")
            if (not isinstance(rows, list) or any(not isinstance(q, str) or not q.strip() for q in rows)
                    or sum(len(q.encode("utf-8")) for q in rows) > 16_384):
                raise RuntimeError("確認質問の形式が合いませんでした。")
            questions.extend(q.strip() for q in rows if q.strip())
        for section in SECTION_KEYS:
            items = result.get(section)
            if not isinstance(items, list):
                raise RuntimeError("生成結果の形式が合いませんでした。")
            for item in items:
                if (not isinstance(item, dict) or set(item) != {"topic", "text", "evidence"}
                        or not isinstance(item["topic"], str) or not isinstance(item["text"], str)):
                    raise RuntimeError("生成結果の形式が合いませんでした。")
                ev = item["evidence"]
                if (not isinstance(ev, list) or not 1 <= len(ev) <= 8
                        or any(not isinstance(x, str) or x not in set(group) | {"C1"} for x in ev)):
                    continue
                # References are context only. Confirmed facts are explicitly separated from transcript evidence.
                if "C1" in ev and not confirmed:
                    continue
                ev = list(dict.fromkeys(ev))
                evidence_text = "\n".join(confirmed if e == "C1" else source[e] for e in ev)
                if not _numbers(item["text"]).issubset(_numbers(evidence_text)):
                    continue
                if section != "open" and _rejected_number(item["text"], evidence_text):
                    continue
                all_items[section].append({"topic": item["topic"], "text": item["text"], "evidence": ev})

    # Remove only exact duplicate claims; no semantic rewriting or merging.
    for sec in SECTION_KEYS:
        seen = set()
        unique = []
        for item in all_items[sec]:
            identity = (item["topic"].strip(), item["text"].strip())
            if identity not in seen:
                seen.add(identity); unique.append(item)
        all_items[sec] = unique

    # For genuinely conflicting cross-chunk next-meeting claims, ask once for
    # an ID-only selection. The model cannot author or rewrite any claim.
    next_items = all_items["next"]
    by_topic: dict[str, list[int]] = {}
    for i, item in enumerate(next_items):
        heading = item["topic"].strip().casefold()
        if re.search(r"日時|日程|予定|開催|^次回$|^定例$", heading) or re.search(
            r"\d{1,2}月\d{1,2}日|[月火水木金土日]曜|\d{1,2}時|来週|翌週", item["text"]
        ):
            heading = "__schedule__"
        by_topic.setdefault(heading, []).append(i)
    conflicts = [indices for indices in by_topic.values() if len(indices) > 1
                 and len({ev for i in indices for ev in next_items[i]["evidence"]}) > 1]
    if conflicts:
        conflict_ids = sorted({i for indices in conflicts for i in indices})
        conflict_evidence = set(ev for i in conflict_ids for ev in next_items[i]["evidence"])
        ordered_evidence = [{"id": ev, "text": value} for ev, value in source.items() if ev in conflict_evidence]
        if "C1" in conflict_evidence:
            ordered_evidence.append({"id": "C1", "text": confirmed})
        data = {"claims": [{"index": i, "topic": next_items[i]["topic"],
                            "text": next_items[i]["text"], "evidence_ids": next_items[i]["evidence"]}
                           for i in conflict_ids], "evidence_in_time_order": ordered_evidence}
        if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > 12_000:
            raise ValueError("会議終盤の相反する予定が大きすぎて安全に照合できません。")
        start = time.monotonic()
        check_cancelled()
        result = call(settings.generation,
            "次回予定を原文の時系列と照合し、最終的に有効な主張のindexをselectedに列挙。"
            "C1は今回人が確認した事実であり、同じ予定で原文と異なる場合は優先。"
            "訂正前の予定や提案は除外。別々の予定なら、根拠を確認できたものをすべて残す。",
            data, SELECT_SCHEMA, 1_024)
        seconds["generation"] += time.monotonic() - start
        selected = result.get("selected")
        valid_selection = (isinstance(selected, list) and all(type(i) is int for i in selected)
                           and len(selected) == len(set(selected))
                           and set(selected).issubset(set(conflict_ids)))
        if not valid_selection:
            raise RuntimeError("予定照合の結果が不正です。安全のため処理を中止しました。")
        selected_set = set(selected)
        conflicting_set = set(conflict_ids)
        all_items["next"] = [it for i, it in enumerate(next_items)
                              if i not in conflicting_set or i in selected_set]

    if settings.verification.provider != "off":
        candidates = [(section, i, item) for section in SECTION_KEYS for i, item in enumerate(all_items[section])]
        approved: set[tuple[str, int]] = set()
        # Batches are bounded by unique evidence text, not repeated full chunks per claim.
        batches, batch, byte_count = [], [], 1_000
        for entry in candidates:
            sec, i, item = entry
            cost = len(json.dumps({"claim": item, "evidence": {ev: source.get(ev, confirmed if ev == "C1" else "") for ev in item["evidence"]}}, ensure_ascii=False).encode("utf-8"))
            if cost + 1_000 > 15_500:
                raise ValueError("検証対象1件が16KBの上限を超えました。")
            if batch and byte_count + cost > 15_500:
                batches.append(batch); batch, byte_count = [], 1_000
            batch.append(entry); byte_count += cost
        if batch:
            batches.append(batch)
        verification_rejected = 0
        for batch in batches:
            check_cancelled()
            evidence_ids = list(dict.fromkeys(ev for _, _, it in batch for ev in it["evidence"]))
            evidence_map = {ev: source.get(ev, confirmed if ev == "C1" else "") for ev in evidence_ids}
            if len(json.dumps(evidence_map, ensure_ascii=False).encode("utf-8")) > 16_000:
                raise ValueError("検証根拠が16KBの上限を超えました。")
            start = time.monotonic()
            result = call(settings.verification,
                "各主張が指定された根拠文に明示的に支えられる場合だけsupported=true。根拠にない推論はfalse。",
                {"claims": [{"index": n, "section": sec, "topic": it["topic"], "text": it["text"], "evidence_ids": it["evidence"]}
                            for n, (sec, i, it) in enumerate(batch)], "evidence": evidence_map}, VERIFY_SCHEMA, 2_048)
            seconds["verification"] += time.monotonic() - start
            rows = result.get("results")
            expected = set(range(len(batch)))
            valid_rows = (isinstance(rows, list) and len(rows) == len(batch)
                          and all(isinstance(r, dict) and set(r) == {"index", "supported"}
                                  and type(r.get("index")) is int and type(r.get("supported")) is bool
                                  for r in rows)
                          and {r["index"] for r in rows} == expected)
            if not valid_rows:
                raise RuntimeError("検証結果が不正です。安全のため処理を中止しました。")
            accepted = {r["index"] for r in rows if r["supported"]}
            verification_rejected += len(batch) - len(accepted)
            approved.update((sec, i) for j, (sec, i, _) in enumerate(batch) if j in accepted)
        for sec in SECTION_KEYS:
            all_items[sec] = [it for i, it in enumerate(all_items[sec]) if (sec, i) in approved]
    else:
        verification_rejected = 0
    clean = validate_minutes({sec: [{"topic": it["topic"], "text": it["text"]} for it in all_items[sec]] for sec in SECTION_KEYS})
    # Exercise the existing display renderer at the workflow boundary.
    render(clean, title, profile)
    return RunResult(clean, corrected, len(parts), seconds, rejected, verification_rejected,
                     tuple(dict.fromkeys(questions)))
