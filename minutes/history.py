"""Opt-in local meeting history. Past minutes are background, not evidence."""
from __future__ import annotations

import os
import sqlite3
import stat
import time
import unicodedata
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Meeting:
    id: str
    revision: int
    title: str
    topic: str
    transcript: str = ""
    body: str = ""
    open_items: str = ""
    corrected: str = ""


def topic_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _label(value: str, field: str, limit: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit or
            any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value)):
        raise ValueError(f"{field}を入力してください（{limit}文字まで）。")
    return value.strip()


class History:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if not stat.S_ISREG(self.path.lstat().st_mode):
                raise ValueError("履歴の保存先が通常のファイルではありません。")
        else:
            os.close(fd)
        connection = sqlite3.connect(self.path, timeout=1.0)
        try:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("""CREATE TABLE IF NOT EXISTS meetings (
                id TEXT PRIMARY KEY, revision INTEGER NOT NULL, title TEXT NOT NULL,
                topic TEXT NOT NULL, topic_key TEXT NOT NULL, transcript TEXT NOT NULL,
                body TEXT NOT NULL, open_items TEXT NOT NULL, corrected TEXT NOT NULL,
                updated INTEGER NOT NULL DEFAULT (unixepoch()))""")
            connection.execute("CREATE INDEX IF NOT EXISTS topic_updated ON meetings(topic_key, updated DESC)")
        except BaseException:
            connection.close()
            raise
        return connection

    def save(self, meeting: Meeting) -> Meeting:
        title, topic = _label(meeting.title, "タイトル", 200), _label(meeting.topic, "話題", 120)
        values = (title, topic, topic_key(topic), meeting.transcript, meeting.body, meeting.open_items, meeting.corrected)
        if any(not isinstance(v, str) or len(v.encode("utf-8")) > 2_000_000 for v in values):
            raise ValueError("会議の履歴保存サイズを超えています。原文はファイルに保存してください。")
        id_ = meeting.id or uuid.uuid4().hex
        stamp = time.time_ns()
        with closing(self._connect()) as con, con:
            if meeting.id:
                cur = con.execute("""UPDATE meetings SET title=?, topic=?, topic_key=?, transcript=?, body=?,
                                    open_items=?, corrected=?, revision=revision+1, updated=?
                                    WHERE id=? AND revision=?""", (*values, stamp, id_, meeting.revision))
                if cur.rowcount != 1:
                    raise ValueError("別のタブで履歴が更新または削除されました。開き直してから保存してください。")
            else:
                con.execute("""INSERT INTO meetings (id, revision, title, topic, topic_key, transcript, body, open_items, corrected, updated)
                             VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""", (id_, *values, stamp))
            saved = con.execute("SELECT id, revision, title, topic, transcript, body, open_items, corrected FROM meetings WHERE id=?",
                                (id_,)).fetchone()
            if saved is None or saved[1] != meeting.revision + 1:
                raise ValueError("保存した版を確認できませんでした。履歴を開き直してください。")
        return Meeting(*saved)

    def get(self, id_: str) -> Meeting | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        with closing(self._connect()) as con:
            row = con.execute("SELECT id, revision, title, topic, transcript, body, open_items, corrected FROM meetings WHERE id=?",
                              (id_,)).fetchone()
            return Meeting(*row) if row else None

    def list(self, topic: str = "") -> list[tuple[str, str, str]]:
        if not self.path.exists() and not self.path.is_symlink():
            return []
        with closing(self._connect()) as con:
            query = "SELECT id, title, topic FROM meetings"
            where = " WHERE topic_key=?" if topic.strip() else ""
            args = (topic_key(topic),) if where else ()
            return con.execute(query + where + " ORDER BY topic_key, updated DESC, id LIMIT 500", args).fetchall()

    def background(self, topic: str, exclude_id: str = "") -> tuple[str, ...]:
        if not topic.strip() or not self.path.exists():
            return ()
        with closing(self._connect()) as con:
            rows = con.execute("""SELECT title, substr(body, 1, 300) FROM meetings WHERE topic_key=? AND id<>?
                                  AND length(trim(body))>0 ORDER BY updated DESC, id LIMIT 3""",
                               (topic_key(topic), exclude_id)).fetchall()
        return tuple(f"過去議事録（今回の事実の根拠にしない）: {title}\n{body}" for title, body in rows)

    def delete(self, id_: str, revision: int) -> None:
        with closing(self._connect()) as con, con:
            if con.execute("DELETE FROM meetings WHERE id=? AND revision=?", (id_, revision)).rowcount != 1:
                raise ValueError("別のタブで履歴が変更されました。開き直してから確認してください。")
