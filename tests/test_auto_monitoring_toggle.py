"""«⏰ Авто-мониторинг» settings toggle must actually gate the scheduler.

Regression for the dead-toggle bug: the button flipped
``addons.algorithm.auto_monitoring`` in state.json but nothing ever
read it — periodic algorithm runs kept firing with the toggle OFF.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch


class AutoMonitoringGateTest(unittest.TestCase):
    def _make_due_slot(self):
        from bot.addons.algorithm.state import Slot
        return Slot(index=1, plan="step", interval_minutes=1.0,
                    last_run_at=time.time() - 3600)

    def test_tick_skipped_when_toggle_off(self):
        from bot.addons.algorithm import scheduler

        slot = self._make_due_slot()
        with patch.object(scheduler, "is_auto_monitoring_enabled", return_value=False), \
             patch("bot.addons.algorithm.state.list_chat_ids", return_value=[111]) as lc, \
             patch("bot.addons.algorithm.state.list_slots", return_value=[slot]), \
             patch.object(scheduler.asyncio, "create_task") as ct:
            asyncio.run(scheduler._tick(bot=object()))
        lc.assert_not_called()
        ct.assert_not_called()

    def test_tick_runs_when_toggle_on(self):
        from bot.addons.algorithm import scheduler

        slot = self._make_due_slot()

        async def _go():
            with patch.object(scheduler, "is_auto_monitoring_enabled", return_value=True), \
                 patch("bot.addons.algorithm.state.list_chat_ids", return_value=[111]), \
                 patch("bot.addons.algorithm.state.list_slots", return_value=[slot]), \
                 patch.object(scheduler, "_run_one") as ro:
                await scheduler._tick(bot=object())
                await asyncio.sleep(0)  # let created task start
                return ro
        ro = asyncio.run(_go())
        ro.assert_called_once()

    def test_default_is_enabled(self):
        """No stored flag → periodic runs stay ON (pre-fix behaviour)."""
        from bot.addons.algorithm import scheduler
        with patch("bot.addons.state.get", return_value=True) as g:
            self.assertTrue(scheduler.is_auto_monitoring_enabled())
        g.assert_called_once_with("algorithm", "auto_monitoring", True)

    def test_settings_label_follows_helper(self):
        """⚙️ Настройки keyboard label mirrors the scheduler helper."""
        from bot import wizard
        with patch("bot.addons.algorithm.scheduler.is_auto_monitoring_enabled",
                   return_value=False):
            kb = wizard._kb_settings_menu(None)
        texts = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("⏰ Авто-мониторинг: ВЫКЛ", texts)


if __name__ == "__main__":
    unittest.main()
