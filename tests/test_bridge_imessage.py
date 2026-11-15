"""Unit tests for MacIMessageBridge read-only chat.db, attachment sips conversion, and osascript argv safety."""

from __future__ import annotations

import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from instinct_mac.bridge_imessage import (
    APPLESCRIPT_SEND_IMESSAGE,
    MacIMessageBridge,
    encode_attributed_body,
)
from instinct_mac.store_sqlite import StateStore


class _FakeProc:
    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return (b"", self._stderr)


class TestMacIMessageBridge(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.chat_db = self.tmp_dir / "chat.db"
        self.store = StateStore(self.tmp_dir / "state.sqlite3")
        self._init_fake_chat_db()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _init_fake_chat_db(self) -> None:
        conn = sqlite3.connect(str(self.chat_db))
        conn.executescript(
            """
            CREATE TABLE handle (
                ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL
            );
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
                guid TEXT,
                handle_id INTEGER,
                text TEXT,
                attributedBody BLOB,
                date INTEGER,
                is_from_me INTEGER DEFAULT 0,
                associated_message_type INTEGER DEFAULT 0
            );
            CREATE TABLE attachment (
                ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT,
                mime_type TEXT
            );
            CREATE TABLE message_attachment_join (
                message_id INTEGER,
                attachment_id INTEGER
            );
            INSERT INTO handle(ROWID, id) VALUES (1, '+8613800138000');
            INSERT INTO handle(ROWID, id) VALUES (2, 'attacker@example.com');
            INSERT INTO message(ROWID, guid, handle_id, text, date, is_from_me, associated_message_type)
                VALUES (1, 'boot-msg', 1, '启动前旧消息', 750000000000000000, 0, 0);
            """
        )
        conn.commit()
        conn.close()

    def test_readonly_mode_and_heic_sips_conversion(self) -> None:
        fake_sips = self.tmp_dir / "fake_sips.sh"
        fake_sips.write_text(
            '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n  if [ "$1" = "--out" ]; then\n    printf "\\xff\\xd8\\xff\\xe0JPEGDATA" > "$2"\n    exit 0\n  fi\n  shift\ndone\nexit 1\n',
            encoding="utf-8",
        )
        fake_sips.chmod(fake_sips.stat().st_mode | stat.S_IXUSR)

        heic_file = self.tmp_dir / "photo.heic"
        heic_file.write_bytes(b"FAKE_HEIC_IMAGE_BYTES")

        bridge = MacIMessageBridge(
            chat_db_path=self.chat_db,
            store=self.store,
            allowed_senders={"+8613800138000"},
            sips_bin=str(fake_sips),
        )
        self.assertEqual(bridge.align_cursor_to_latest(), 1)

        with bridge._connect_readonly() as ro_conn:
            with self.assertRaises(sqlite3.OperationalError):
                ro_conn.execute("INSERT INTO handle(id) VALUES ('x')")

        conn = sqlite3.connect(str(self.chat_db))
        conn.execute(
            "INSERT INTO message(ROWID, guid, handle_id, text, date, is_from_me, associated_message_type) VALUES (2, 'tap-1', 1, 'Liked', 750000010000000000, 0, 2001)"
        )
        conn.execute(
            "INSERT INTO message(ROWID, guid, handle_id, text, date, is_from_me, associated_message_type) VALUES (3, 'unauth-1', 2, 'hi', 750000020000000000, 0, 0)"
        )
        conn.execute(
            "INSERT INTO message(ROWID, guid, handle_id, text, attributedBody, date, is_from_me, associated_message_type) VALUES (4, 'img-1', 1, '\ufffc', ?, 750000030000000000, 0, 0)",
            (encode_attributed_body("\ufffc帮我看下这张机票截图"),),
        )
        conn.execute(
            "INSERT INTO attachment(ROWID, filename, mime_type) VALUES (1, ?, 'image/heic')",
            (str(heic_file),),
        )
        conn.execute(
            "INSERT INTO message_attachment_join(message_id, attachment_id) VALUES (4, 1)"
        )
        conn.commit()
        conn.close()

        msgs = bridge.poll_inbound()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].rowid, 4)
        self.assertEqual(msgs[0].text, "帮我看下这张机票截图")
        self.assertEqual(len(msgs[0].image_data_urls), 1)
        self.assertTrue(msgs[0].image_data_urls[0].startswith("data:image/jpeg;base64,"))

    async def test_parameterized_osascript_chunking_and_injection_safety(self) -> None:
        bridge = MacIMessageBridge(
            chat_db_path=self.chat_db,
            store=self.store,
            allowed_senders={"+8613800138000"},
        )
        recorded_execs: list[tuple[str, ...]] = []

        async def fake_exec(*args: str, **kwargs: object) -> _FakeProc:
            recorded_execs.append(tuple(str(a) for a in args))
            return _FakeProc(returncode=0)

        dangerous_text = ('" & do shell script "rm -rf /" & "' + "\n") + ("长消息分片A" * 800)
        self.assertGreater(len(dangerous_text), 3500)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await bridge.send_text("+8613800138000", dangerous_text)

        self.assertGreaterEqual(len(recorded_execs), 2)
        for call_args in recorded_execs:
            self.assertEqual(call_args[0], "/usr/bin/osascript")
            self.assertEqual(call_args[1], "-e")
            self.assertEqual(call_args[2], APPLESCRIPT_SEND_IMESSAGE)
            self.assertEqual(call_args[3], "+8613800138000")
            self.assertLessEqual(len(call_args[4]), 3500)


if __name__ == "__main__":
    unittest.main()
