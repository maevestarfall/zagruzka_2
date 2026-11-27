from __future__ import annotations

import sqlite3

DB_PATH = "data/coder.db"

def render_research_markdown() -> str:
    """Generate deterministic markdown digest from coder_external_ref_decisions table."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT ref_id, ref_url, integration_mode, applies_to_cluster, decided_at FROM coder_external_ref_decisions")
    rows = cursor.fetchall()
    conn.close()
    
    lines = ["# 🔬 Сводка Исследований Внешних Референсов (Researcher Digest)", ""]
    if not rows:
        lines.append("Нет записанных решений по референсам.")
    else:
        for r in rows:
            lines.append(f"### Референс #{r[0]}: {r[1]}")
            lines.append(f"- **Режим интеграции:** `{r[2]}`")
            lines.append(f"- **Применимо к кластеру:** `{r[3]}`")
            lines.append(f"- **Решение вынесено:** {r[4]}")
            lines.append("")
            
    return "\n".join(lines)
