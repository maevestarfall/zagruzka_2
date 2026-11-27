# lilush v17.18 → big feature build

Source: lilush-bot-v17.18-vibe-completed (14).zip

## Features requested
- [ ] A. OSINT main-menu button «🕵️ Исследование и поиск человека» (addon `osint`) from Awesome-AI-OSINT
- [ ] B. «Источники» (prompt-library sites) in photo/video gen — add/remove sites; «Помощь ИИ» → 4 variants (3 verbatim popular + 1 hybrid)
- [ ] C. Settings button «🎯 Приоритет» — on/off + mode (only_bot / bot_then_internet / instruction-tree)
- [ ] D. Hermes experience-document: record lessons FROM EXPERIENCE, export one button

## Engineering
- [ ] baseline audit (ruff F821/F811, import all, pytest subset)
- [ ] re-apply recurring fixes if present (ParseMode import, _kb_main_after_claim, mailbox/models.py)
- [ ] wire main-menu + settings buttons + cb dispatch + build_addon_routers
- [ ] tests for each new addon
- [ ] build zip via python zipfile, deliver
