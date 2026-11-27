"""Tests for the memory-screen addon (main-menu UI shim around
``storage.append_history`` / ``storage.clear_history``).
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock


class MemoryAddonTest(unittest.TestCase):
    """Verifies the memory addon reads from and clears the same history
    storage that ``run_agent`` uses, so the count the user sees on the
    screen matches what the LLM actually receives in context.
    """

    def setUp(self):
        # Wipe the live storage's history dict between tests so per-user
        # state is isolated. We reuse the same `storage` singleton that
        # ``bot.addons.memory.handlers`` reads from — reloading modules
        # at test time desync's it from already-imported handlers.
        from bot.storage import storage as _storage

        self.storage = _storage
        # Snapshot existing _state so we can restore it on teardown.
        self._state_snapshot = {
            k: v for k, v in self.storage._state.items() if k != "_settings"
        }
        for k in list(self.storage._state.keys()):
            if k != "_settings":
                del self.storage._state[k]

    def tearDown(self):
        # Restore.
        for k in list(self.storage._state.keys()):
            if k != "_settings":
                del self.storage._state[k]
        self.storage._state.update(self._state_snapshot)

    def test_show_screen_counts_user_and_assistant(self):
        """After appending 3 user + 3 assistant turns, the screen shows
        the same counts.
        """
        from bot.addons.memory.handlers import show_screen

        # Seed some history under user 42.
        for i in range(3):
            self.storage.append_history(
                42, {"role": "user", "content": f"q{i}"}
            )
            self.storage.append_history(
                42, {"role": "assistant", "content": f"a{i}"}
            )

        msg = MagicMock()
        msg.from_user.id = 42
        msg.answer = AsyncMock()

        asyncio.run(show_screen(msg))

        msg.answer.assert_awaited()
        sent_text = msg.answer.await_args.args[0]
        self.assertIn("<b>3</b> ваших", sent_text)
        self.assertIn("<b>3</b> ответов бота", sent_text)
        # Clear button must appear when history is non-empty.
        kb = msg.answer.await_args.kwargs["reply_markup"]
        labels = [
            btn.text for row in kb.inline_keyboard for btn in row
        ]
        self.assertIn("🗑 Очистить память", labels)

    def test_clear_callback_wipes_history(self):
        from bot.addons.memory.handlers import build_memory_router

        # Seed something so there's history to clear.
        self.storage.append_history(7, {"role": "user", "content": "hi"})
        self.assertTrue(self.storage.get_history(7))

        router = build_memory_router()
        on_clear = None
        for h in router.observers["callback_query"].handlers:
            if h.callback.__name__ == "on_clear":
                on_clear = h.callback
                break
        self.assertIsNotNone(on_clear)

        query = MagicMock()
        query.from_user.id = 7
        query.message = MagicMock()
        query.message.edit_text = AsyncMock()
        query.message.answer = AsyncMock()
        query.answer = AsyncMock()

        asyncio.run(on_clear(query))

        # History is gone.
        self.assertEqual(self.storage.get_history(7), [])
        query.answer.assert_awaited()


class FilesScreenTest(unittest.TestCase):
    """The «📂 Файлы» button + screen: sizes, free space, and wiring."""

    def test_human_size_formats(self):
        from bot.addons.memory.handlers import _human_size

        self.assertEqual(_human_size(0), "0 B")
        self.assertEqual(_human_size(512), "512 B")
        self.assertEqual(_human_size(1536), "1.5 KB")
        self.assertEqual(_human_size(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(_human_size(3 * 1024**3), "3.0 GB")

    def test_dir_stats_counts_and_sizes(self):
        import tempfile
        from pathlib import Path

        from bot.addons.memory.handlers import _dir_stats

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.mp4").write_bytes(b"x" * 100)
            sub = root / "job1"
            sub.mkdir()
            (sub / "b.mp4").write_bytes(b"y" * 200)
            count, total = _dir_stats(root)
            self.assertEqual(count, 2)
            self.assertEqual(total, 300)

        # Missing dir → zeros, no crash.
        self.assertEqual(_dir_stats(Path("/no/such/dir/xyz")), (0, 0))

    def test_list_media_files_filters_and_orders(self):
        import os
        import tempfile
        from pathlib import Path

        from bot.addons.memory.handlers import _list_media_files

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "old.mp4").write_bytes(b"x" * 10)
            (root / "new.mp4").write_bytes(b"x" * 20)
            (root / "notes.txt").write_bytes(b"ignore me")  # non-video skipped
            # Make "new.mp4" newer than "old.mp4".
            os.utime(root / "old.mp4", (1000, 1000))
            os.utime(root / "new.mp4", (2000, 2000))
            media = _list_media_files(root)
            names = [n for n, _ in media]
            self.assertEqual(names, ["new.mp4", "old.mp4"])  # newest first
            self.assertNotIn("notes.txt", names)

    def test_files_screen_text_has_categories_and_free_space(self):
        from bot.addons.memory.handlers import _files_manager_screen

        text, _kb = _files_manager_screen("memory")
        self.assertIn("📂 Хранилище файлов бота", text)
        self.assertIn("Скачанные файлы", text)
        self.assertIn("Готовые клипы", text)
        self.assertIn("Всего занято", text)
        self.assertIn("Свободно на диске", text)

    def test_files_button_present_in_keyboard(self):
        from bot.addons.memory.handlers import _kb_screen

        for has_history in (True, False):
            kb = _kb_screen(has_history=has_history)
            labels = [b.text for row in kb.inline_keyboard for b in row]
            self.assertIn("📂 Файлы", labels)

    def test_files_callback_renders_screen(self):
        from bot.addons.memory.handlers import build_memory_router

        router = build_memory_router()
        on_files = None
        for h in router.observers["callback_query"].handlers:
            if h.callback.__name__ == "on_files":
                on_files = h.callback
                break
        self.assertIsNotNone(on_files)

        query = MagicMock()
        query.message = MagicMock()
        query.message.edit_text = AsyncMock()
        query.answer = AsyncMock()

        asyncio.run(on_files(query))

        query.message.edit_text.assert_awaited()
        sent = query.message.edit_text.await_args.args[0]
        self.assertIn("📂 Хранилище файлов бота", sent)


if __name__ == "__main__":
    unittest.main()
