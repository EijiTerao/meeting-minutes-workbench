import unittest
from unittest.mock import patch

from minutes.routes import Settings, Stage
from minutes.workflow import run_workflow


def payload(items=()):
    return {key: list(items) if key == "decisions" else []
            for key in ("decisions", "owners", "deadlines", "next", "open")}


class WorkflowTests(unittest.TestCase):
    def test_long_tail_survives_and_transcript_is_not_mutated(self):
        transcript = ("冒頭の発言。\n" * 5000) + "会議の最後に納期を12月に決定した。"
        original = transcript
        seen = []

        def call(stage, system, data, schema, authorized, max_output):
            seen.extend(row["text"] for row in data.get("sources", []))
            out = payload()
            tail = next((row["id"] for row in data.get("sources", []) if "会議の最後" in row["text"]), None)
            if tail:
                out["decisions"] = [{"topic": "納期", "text": "納期を12月に決定した", "evidence": [tail]}]
            return out

        settings = Settings(generation=Stage("ollama", "local-model"))
        with patch("minutes.workflow._call", side_effect=call):
            result = run_workflow(transcript, "定例", settings, {})
        self.assertEqual(transcript, original)
        self.assertEqual("".join(seen), transcript)
        self.assertGreater(result.chunks, 1)
        self.assertTrue(result.minutes["decisions"])

    def test_openai_auth_preflight_prevents_any_call(self):
        settings = Settings(generation=Stage("openai", "model"))
        with patch("minutes.workflow._call") as call:
            with self.assertRaises(ValueError):
                run_workflow("発言", "会議", settings, {})
        call.assert_not_called()

    def test_disabled_generation_fails_and_off_stages_are_separate(self):
        with self.assertRaises(ValueError):
            run_workflow("発言", "会議", Settings(), {})
        settings = Settings(correction=Stage("off"), generation=Stage("ollama", "m"), verification=Stage("off"))
        out = payload([{"topic": "論点", "text": "確認した", "evidence": ["S1"]}])
        with patch("minutes.workflow._call", return_value=out) as call:
            result = run_workflow("確認した。", "会議", settings, {})
        self.assertEqual(result.corrected, "確認した。")
        self.assertEqual(call.call_count, 1)

    def test_missing_and_unsupported_evidence_are_dropped(self):
        settings = Settings(generation=Stage("ollama", "m"))
        out = payload([{"topic": "論点", "text": "根拠不明", "evidence": ["S99"]}])
        with patch("minutes.workflow._call", return_value=out):
            result = run_workflow("発言。", "会議", settings, {})
        self.assertEqual(result.minutes["decisions"], [])

    def test_correction_cannot_change_numbers_or_negation(self):
        settings = Settings(correction=Stage("ollama", "m"), generation=Stage("ollama", "m"))
        def call(stage, system, data, schema, authorized, max_output):
            if "chunks" in data:
                return {"chunks": [{"id": row["id"], "text": "金額は20円ではない。"} for row in data["chunks"]]}
            return payload()
        with patch("minutes.workflow._call", side_effect=call):
            result = run_workflow("金額は10円ではない。", "会議", settings, {})
        self.assertEqual(result.corrected, "金額は10円ではない。")
        self.assertEqual(result.corrections_rejected, 1)

    def test_correction_preserves_an_explicit_decision_to_defer(self):
        from minutes.workflow import _safe_correction

        self.assertFalse(_safe_correction(
            "来月の販促予算の承認を見送ることを決定した。",
            "来月の販促予算の承認を進めることを決定した。",
        ))
        self.assertFalse(_safe_correction(
            "来月の販促費は取引先へ請求することを決定した。",
            "来月の販促費は取引先へ支払うことを決定した。",
        ))

    def test_supported_id_alone_does_not_admit_new_numeric_claim(self):
        settings = Settings(generation=Stage("ollama", "m"))
        out = payload([{"topic": "印刷費", "text": "上限は3万円", "evidence": ["S1"]}])
        with patch("minutes.workflow._call", return_value=out):
            result = run_workflow("上限は8千円とする。", "会議", settings, {})
        self.assertFalse(result.minutes["decisions"])

    def test_rejected_budget_is_not_accepted_just_because_amount_exists(self):
        settings = Settings(generation=Stage("ollama", "m"))
        out = payload([{"topic": "予算", "text": "予算200万円を承認した", "evidence": ["S1"]},
                       {"topic": "予算", "text": "予算20万円に決定した", "evidence": ["S1"]}])
        with patch("minutes.workflow._call", return_value=out):
            result = run_workflow("予算200万円の案は承認せず、20万円に決定した。", "会議", settings, {})
        self.assertEqual(result.minutes["decisions"], [{"topic": "予算", "text": "予算20万円に決定した"}])

    def test_granular_sources_and_cross_chunk_conflicting_next_are_reconciled(self):
        transcript = "次回は火曜。\n" + ("補足の発言。\n" * 200) + "次回は水曜に変更。"
        settings = Settings(generation=Stage("ollama", "m"))
        calls = []
        def call(stage, system, data, schema, authorized, max_output):
            if "sources" in data:
                calls.append(data)
                rows = data["sources"]
                out = payload()
                for row in rows:
                    if "火曜" in row["text"]:
                        out["next"].append({"topic": "次回日程", "text": "次回は火曜", "evidence": [row["id"]]})
                    if "水曜" in row["text"]:
                        out["next"].append({"topic": "次回予定", "text": "次回は水曜に変更", "evidence": [row["id"]]})
                return out
            self.assertIn("時系列と照合", system)
            self.assertIn("水曜に変更", data["evidence_in_time_order"][-1]["text"])
            return {"selected": [1]}
        with patch("minutes.workflow._call", side_effect=call):
            result = run_workflow(transcript, "会議", settings, {})
        self.assertGreater(len({row["id"] for data in calls for row in data["sources"]}), 1)
        self.assertEqual(result.minutes["next"], [{"topic": "次回予定", "text": "次回は水曜に変更"}])

    def test_empty_title_fails_before_provider(self):
        settings = Settings(generation=Stage("ollama", "m"))
        with patch("minutes.workflow._call") as call:
            with self.assertRaises(ValueError):
                run_workflow("発言", "  ", settings, {})
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
