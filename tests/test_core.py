import json
import tempfile
import unittest
from pathlib import Path

from minutes.core import DEFAULT_PROFILE, load_profile, render, validate_minutes


def sample():
    return {
        "decisions": [{"topic": "予算", "text": "上限を設定する"}],
        "owners": [{"topic": "予算", "text": "Aさんが見積を確認する"}],
        "deadlines": [{"topic": "予算", "text": "金曜まで"}],
        "next": [{"topic": "予算", "text": "金曜まで"}, {"topic": "日程", "text": "次回は11月20日"}],
        "open": [{"topic": "会場", "text": "候補を確認中"}],
    }


class CoreTests(unittest.TestCase):
    def test_default_render_and_open_separate(self):
        body, open_items = render(sample(), "会議メモ")
        self.assertIn("会議メモ", body)
        self.assertIn("【決定事項】", body)
        self.assertIn("【次回】", body)
        self.assertEqual(body.count("金曜まで"), 1)
        self.assertNotIn("未決事項", body)
        self.assertIn("【未決事項】", open_items)
        self.assertIn("候補を確認中", open_items)

    def test_partial_custom_profile_order_and_brackets(self):
        profile = {"labels": {"owners": "担当者"}, "order": ["next", "owners", "deadlines", "decisions"],
                   "section_brackets": ["[", "]"], "topic_brackets": ["<", ">"], "bullet": "-"}
        body, _ = render(sample(), "タイトル", profile)
        self.assertLess(body.index("[次回]"), body.index("[担当者]"))
        self.assertIn("<予算>", body)
        self.assertIn("- 上限を設定する", body)

    def test_profile_json_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps({"labels": {"next": "次の会議"}}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(load_profile(path)["labels"]["next"], "次の会議")
        self.assertEqual(load_profile(None), DEFAULT_PROFILE)

    def test_empty_sections_hidden(self):
        payload = {key: [] for key in sample()}
        body, open_items = render(payload, "空の議事録")
        self.assertEqual(body, "空の議事録")
        self.assertEqual(open_items, "空の議事録")

    def test_invalid_shape_types_and_unknown_fields(self):
        for value in (None, [], {}, {**sample(), "extra": []}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_minutes(value)
        bad = sample()
        bad["decisions"] = [{"topic": "x", "text": "y", "other": 1}]
        with self.assertRaises(ValueError):
            validate_minutes(bad)
        bad = sample()
        bad["owners"] = "not a list"
        with self.assertRaises(ValueError):
            validate_minutes(bad)

    def test_reject_controls_newlines_and_limits(self):
        for text in ("line one\nline two", "x\u202ey", "x\x00y"):
            bad = sample()
            bad["decisions"] = [{"topic": "x", "text": text}]
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                validate_minutes(bad)
        bad = sample()
        bad["decisions"] = [{"topic": "x" * 121, "text": "x"}]
        with self.assertRaises(ValueError):
            validate_minutes(bad)
        bad = sample()
        bad["decisions"] = [{"topic": "x", "text": "x" * 2001}]
        with self.assertRaises(ValueError):
            validate_minutes(bad)
        bad = {key: [{"topic": "x", "text": "y"}] * 41 for key in sample()}
        with self.assertRaises(ValueError):
            validate_minutes(bad)


if __name__ == "__main__":
    unittest.main()
