"""Version-diff addon (``versiff``) — compare functions/classes across project
zip archives against a chosen reference version and produce a status table.

Separate from the Slack connector: the Slack addon only *delivers* the zip
files into storage; this addon analyses them. It walks each archive, parses
every ``.py`` with the stdlib ``ast`` (no third-party deps), collects every
function / method / class and compares it to the reference archive, marking
each as добавлена / удалена / изменилась / без изменений.
"""

from __future__ import annotations
