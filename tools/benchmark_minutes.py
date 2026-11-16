"""Compare exported default-style minutes for the bundled synthetic meeting."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import unicodedata
from pathlib import Path


def _normalize(text: str) -> str:
    return re.sub(r"[\s,、]", "", unicodedata.normalize("NFKC", text))


def _sections(body: str) -> dict[str, str]:
    matches = list(re.finditer(r"^【([^\n】]+)】\s*$", body, re.MULTILINE))
    return {m.group(1): _normalize(body[m.end(): matches[i + 1].start() if i < len(matches) - 1 else len(body)])
            for i, m in enumerate(matches)}


def make_long_fixture(short: str) -> str:
    """Put the same facts several context windows apart, without new decisions."""
    rows = short.splitlines()
    if len(rows) != 7:
        raise ValueError("同梱の架空会議テキストを指定してください。")
    filler = (
        "休憩前に各自が見た展示の感想を順番に述べた。"
        "話題は展示を見たときの印象だけで、新たな決定・依頼・日程変更はなかった。\n"
    )
    gap = (filler * 110)
    return "\n".join(rows[:2]) + "\n" + gap + "\n".join(rows[2:4]) + "\n" + gap + "\n".join(rows[4:]) + "\n"


def score(location: Path) -> dict[str, bool]:
    def read(name: str) -> str:
        with (location / name).open("rb") as handle:
            raw = handle.read(262_145)
        if len(raw) > 262_144:
            raise ValueError("比較する議事録が大きすぎます。")
        return raw.decode("utf-8-sig")

    body, pending = read("minutes.txt"), read("open-issues.txt")
    sections = _sections(body)
    decision = sections.get("決定事項", "")
    owners, deadline = sections.get("担当", ""), sections.get("期限", "")
    schedule = sections.get("次回", "")
    open_items = _sections(pending).get("未決事項", "")
    body_text = _normalize(body)
    return {
        "placement": "入口" in decision and bool(re.search(r"階段(?:の)?横", decision)),
        "negative_preserved": bool(re.search(r"出口[^。]{0,32}(しない|掲示しない|置かない|対象外|掲示せず)", decision)),
        "budget": bool(re.search(r"上限[^。]{0,20}(8000|8千)円|(8000|8千)円[^。]{0,20}上限", decision)),
        "owner": "制作班" in owners and "初稿" in owners,
        "deadline": "4月16日" in deadline and "初稿" in deadline,
        "late_decision_owner": "受付班" in owners and "掲示前" in owners and bool(re.search(r"流れ|動線", owners)),
        "next_only": all(word in schedule for word in ("4月18日", "14時", "資料室"))
        and body_text.count("4月18日") == 1 and "4月18日" not in _normalize(pending),
        "unresolved_separation": "多言語" in open_items and "多言語" not in body and "【未決事項】" not in body,
        "superseded_not_present": not any(word in body_text + _normalize(pending) for word in ("4月10日", "1万円", "10000円")),
    }


def timing_record(location: Path) -> dict | None:
    timing = location / "timing.json"
    if not timing.is_file():
        return None
    with timing.open("rb") as handle:
        raw = handle.read(16_001)
    if len(raw) > 16_000:
        raise ValueError("時間記録が大きすぎます。")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("時間記録の形式が不正です。")
    return data


def _benchmark_route(folders: list[Path]) -> dict[str, str]:
    """Validate that every measured run declares one identical inference route."""
    routes = []
    for folder in folders:
        record = timing_record(folder)
        if record is None:
            raise ValueError("厳格比較の全反復に生成経路の記録が必要です。")
        provider, model = record.get("generation_provider"), record.get("generation_model")
        if provider not in ("ollama", "openai") or not isinstance(provider, str):
            raise ValueError("厳格比較の生成経路（provider/model）を確認してください。")
        if (not isinstance(model, str) or not model or model != model.strip()
                or any(char.isspace() for char in model)):
            raise ValueError("厳格比較の生成経路（provider/model）を確認してください。")
        routes.append((provider, model))
    if not routes or len(set(routes)) != 1:
        raise ValueError("全反復で同じ生成経路（provider/model）を指定してください。")
    return {"provider": routes[0][0], "model": routes[0][1]}


def elapsed(location: Path, explicit: float | None) -> float | None:
    if explicit is not None:
        value = explicit
    else:
        record = timing_record(location)
        if record is None:
            return None
        value = record["seconds"]["workflow"]
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("所要時間は正の秒数を指定してください。")
    return float(value)


def _series_stats(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    return {"runs": len(values), "median_seconds": round(statistics.median(values), 3),
            "p95_seconds": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3)}


def main() -> int:
    parser = argparse.ArgumentParser(description="同じ架空会議から出した本文・未決事項を比較します（実会議には使わない）。")
    parser.add_argument("--baseline", type=Path, help="従来版で同じ入力・既定書式から保存した本文と未決事項のフォルダ")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--baseline-seconds", type=float)
    parser.add_argument("--candidate-seconds", type=float)
    parser.add_argument("--baseline-run", type=Path, action="append", default=[],
                        help="同じ条件で反復した従来版の出力。timing.jsonを含む")
    parser.add_argument("--candidate-run", type=Path, action="append", default=[],
                        help="同じ条件で反復した候補版の出力。timing.jsonを含む")
    parser.add_argument("--write-long-fixture", type=Path, metavar="NEW_PATH")
    parser.add_argument("--require-parity", action="store_true",
                        help="両方5回以上の反復で固定評価と相対速度の暫定基準を検査する")
    args = parser.parse_args()
    fixture = Path(__file__).resolve().parents[1] / "examples" / "benchmark-transcript.txt"
    try:
        if args.write_long_fixture:
            with args.write_long_fixture.open("x", encoding="utf-8", newline="\n") as destination:
                destination.write(make_long_fixture(fixture.read_text(encoding="utf-8")))
            return 0
        if args.candidate is None:
            parser.error("--candidate または --write-long-fixture を指定してください。")
        candidate = score(args.candidate)
        baseline = score(args.baseline) if args.baseline else None
        candidate_seconds = elapsed(args.candidate, args.candidate_seconds)
        baseline_seconds = elapsed(args.baseline, args.baseline_seconds) if args.baseline else None
        if len(args.baseline_run) != len(args.candidate_run):
            raise ValueError("従来版と候補版の反復回数を揃えてください。")
        all_runs = args.baseline_run + args.candidate_run
        if len({folder.resolve() for folder in all_runs}) != len(all_runs):
            raise ValueError("同じ出力フォルダを複数の実測として指定できません。")
        route = None
        if args.require_parity:
            if args.baseline_seconds is not None or args.candidate_seconds is not None:
                raise ValueError("厳格比較では反復記録の時間を使用してください。")
            if args.candidate.resolve() not in {p.resolve() for p in args.candidate_run}:
                raise ValueError("候補の主出力は候補反復の一つを指定してください。")
            if args.baseline is not None and args.baseline.resolve() not in {p.resolve() for p in args.baseline_run}:
                raise ValueError("従来版の主出力は従来版反復の一つを指定してください。")
        if args.require_parity and all_runs:
            route = _benchmark_route(all_runs)
            short = fixture.read_text(encoding="utf-8")
            fixture_digests = {hashlib.sha256(text.encode("utf-8")).hexdigest()
                               for text in (short, make_long_fixture(short))}
            measured_digests = {
                (timing_record(folder) or {}).get("source_sha256") for folder in all_runs
            }
            if len(measured_digests) != 1 or not measured_digests.issubset(fixture_digests):
                raise ValueError("全実測の入力を、同一の同梱合成素材で揃えてください。")
        runs = {}
        for key, folders in (("baseline", args.baseline_run), ("candidate", args.candidate_run)):
            if folders:
                values = [elapsed(folder, None) for folder in folders]
                if any(value is None for value in values):
                    raise ValueError("すべての反復に timing.json が必要です。")
                runs[key] = {**_series_stats(values),
                             "quality_passes": sum(all(score(folder).values()) for folder in folders)}
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        parser.exit(2, f"比較できませんでした。入力ファイル・書式・時間を確認してください（{type(exc).__name__}）。\n")
    ratio = candidate_seconds / baseline_seconds if candidate_seconds and baseline_seconds else None
    parity = None
    if args.require_parity:
        prior, new = runs.get("baseline", {}), runs.get("candidate", {})
        measured = prior.get("runs", 0) >= 5 and new.get("runs", 0) >= 5
        parity = {
            "measured": measured,
            "quality": bool(measured and new["quality_passes"] == new["runs"]),
            "median": bool(measured and new["median_seconds"] <= 1.20 * prior["median_seconds"]),
            "p95": bool(measured and new["p95_seconds"] <= 1.35 * prior["p95_seconds"]),
        }
    print(json.dumps({
        "fixture": "exhibition-planning",
        "baseline": baseline,
        "candidate": candidate,
        "baseline_seconds": baseline_seconds,
        "candidate_seconds": candidate_seconds,
        "elapsed_ratio": ratio,
        "repeated_runs": runs,
        "provisional_benchmark_gate": parity,
        "benchmark_route": route,
        "note": "固定語句による補助検査。新規の作り話、読みやすさ、他の会議や実機での性能は別途確認。",
    }, ensure_ascii=False, indent=2))
    return 0 if all(candidate.values()) and (parity is None or all(parity.values())) else 1


if __name__ == "__main__":
    raise SystemExit(main())
