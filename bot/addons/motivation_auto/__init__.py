"""Bot-wide auto-motivation (context injection).

Uses the same document stored in debates.motivation but applies it
to the main bot chat. The injection is transparent — a short context
phrase is prepended to the system prompt every N messages.

Settings (stored via storage):
  - mode: "auto" | "manual" (default "auto")
  - interval: int (0 = use default formula based on memory)
  - msg_counter: int (cumulative meaningful messages since last injection)
"""
