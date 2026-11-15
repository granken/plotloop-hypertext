"""Unit tests for StateStore SQLite FTS5 hybrid search, backfill, and crash history sanitization."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from hypertext.store_sqlite import StateStore


class TestStoreFTS5AndHistory(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test_state.sqlite3"
        self.store = StateStore(self.db_path)
        self.sender = "+86 138-0013-8000"

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_fts5_hybrid_chinese_english_and_delete(self) -> None:
        self.store.save_memory(
            self.sender,
            "coffee_pref",
            "去上海虹桥T2坐早班机必喝 Starbucks 冰美式去冰",
            tags="咖啡,虹桥,出行",
        )
        self.store.save_memory(
            self.sender,
            "seat_pref",
            "京沪高铁二等座只选靠窗F座",
            tags="高铁,座位",
        )
        self.store.save_memory(
            self.sender,
            "diet_note",
            "晚餐少碳水，不吃香菜",
            tags="饮食",
        )

        res1 = self.store.search_memories("+8613800138000", "虹桥 冰美式", limit=5)
        self.assertGreaterEqual(len(res1), 1)
        self.assertEqual(res1[0]["key"], "coffee_pref")

        res2 = self.store.search_memories("+8613800138000", "靠窗", limit=5)
        self.assertGreaterEqual(len(res2), 1)
        self.assertEqual(res2[0]["key"], "seat_pref")

        res3 = self.store.search_memories("+8613800138000", "starb", limit=5)
        self.assertGreaterEqual(len(res3), 1)
        self.assertEqual(res3[0]["key"], "coffee_pref")

        all_mems = self.store.search_memories("+8613800138000")
        self.assertEqual(len(all_mems), 3)

        self.assertTrue(self.store.delete_memory("+8613800138000", "diet_note"))
        self.assertFalse(self.store.delete_memory("+8613800138000", "diet_note"))
        self.assertEqual(self.store.search_memories("+8613800138000", "香菜"), [])

    def test_automatic_fts_backfill_and_embedding_fusion(self) -> None:
        now = time.time()
        self.store._conn.execute(
            """
            INSERT INTO memories(sender, mem_key, mem_value, tags, embedding_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "+8613800138000",
                "legacy_flight",
                "深圳宝安机场习惯走贵宾安检通道",
                "机场,深圳",
                "[1.0, 0.0, 0.0]",
                now,
                now,
            ),
        )
        self.store._conn.commit()

        hits = self.store.search_memories(self.sender, "宝安机场")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["key"], "legacy_flight")

        sem_hits = self.store.search_memories(
            self.sender, query="无关字面词", limit=5, query_embedding=[0.98, 0.02, 0.0]
        )
        self.assertEqual(len(sem_hits), 1)
        self.assertEqual(sem_hits[0]["key"], "legacy_flight")

    def test_thinking_chain_persistence_and_crash_sanitization(self) -> None:
        self.store.append_conversation_message(self.sender, "user", "帮我查记忆")
        self.store.append_conversation_message(
            self.sender,
            "assistant",
            "",
            tool_calls=[
                {
                    "id": "tc_valid_1",
                    "type": "function",
                    "function": {"name": "search_memory", "arguments": '{"query":"虹桥"}'},
                }
            ],
            reasoning_content="正在检索虹桥相关偏好",
            server_tool_calls=[{"id": "srv_9", "type": "deepseek_web_search"}],
        )
        self.store.append_conversation_message(
            self.sender,
            "tool",
            '{"ok":true}',
            tool_call_id="tc_valid_1",
            name="search_memory",
        )
        self.store.append_conversation_message(
            self.sender,
            "assistant",
            "您偏好冰美式。",
            reasoning_content="",
        )

        self.store.append_conversation_message(self.sender, "user", "崩溃测试请求")
        self.store.append_conversation_message(
            self.sender,
            "assistant",
            "",
            tool_calls=[
                {
                    "id": "tc_crash_a",
                    "type": "function",
                    "function": {"name": "search_memory", "arguments": "{}"},
                },
                {
                    "id": "tc_crash_b",
                    "type": "function",
                    "function": {"name": "list_scheduled_tasks", "arguments": "{}"},
                },
            ],
            reasoning_content="半截崩溃思考",
        )
        self.store.append_conversation_message(
            self.sender,
            "tool",
            '{"ok":true}',
            tool_call_id="tc_crash_a",
            name="search_memory",
        )
        self.store.append_conversation_message(self.sender, "user", "恢复后的新消息")

        ds_hist = self.store.get_conversation_history(
            self.sender, limit=40, is_deepseek_native=True
        )
        roles = [m["role"] for m in ds_hist]
        self.assertEqual(
            roles, ["user", "assistant", "tool", "assistant", "user", "user"]
        )
        self.assertEqual(ds_hist[1]["tool_calls"][0]["id"], "tc_valid_1")
        self.assertEqual(ds_hist[1]["reasoning_content"], "正在检索虹桥相关偏好")
        self.assertEqual(ds_hist[1]["server_tool_calls"][0]["id"], "srv_9")
        self.assertIn("reasoning_content", ds_hist[3])
        self.assertEqual(ds_hist[3]["reasoning_content"], "")

        oa_hist = self.store.get_conversation_history(
            self.sender, limit=40, is_deepseek_native=False
        )
        for m in oa_hist:
            self.assertNotIn("reasoning_content", m)
            self.assertNotIn("server_tool_calls", m)

    def test_scheduled_tasks_oneshot_and_recurring(self) -> None:
        now = 1700000000.0
        t1 = self.store.add_scheduled_task(
            self.sender, "direct_notify", "一次性提醒", run_at=now - 10, interval_seconds=0
        )
        t2 = self.store.add_scheduled_task(
            self.sender, "agent_wakeup", "周期巡检", run_at=now - 5, interval_seconds=60.0
        )
        due = self.store.claim_due_tasks(now_ts=now)
        self.assertEqual([d["id"] for d in due], [t1, t2])

        active = self.store.list_scheduled_tasks(sender=self.sender, only_enabled=True)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], t2)
        self.assertAlmostEqual(active[0]["run_at"], now + 60.0)

        self.assertTrue(self.store.cancel_scheduled_task(t2, sender=self.sender))
        self.assertEqual(
            self.store.list_scheduled_tasks(sender=self.sender, only_enabled=True), []
        )


if __name__ == "__main__":
    unittest.main()
