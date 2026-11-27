"""The browser macro-RECORDER userscript (Tampermonkey) served by the bot.

Records clicks + copies on any site, replays them, and exports a macro in
the exact ``Клик: / Пауза: / Скопировать:`` format the manual clicker
understands — so the owner records on a real page and pastes the result
straight back into Telegram.
"""

from __future__ import annotations

RECORDER_FILENAME = "chkr_macro_recorder.user.js"

# Kept as a normal triple-quoted string (JS braces are literal). Validated
# with `node --check` in tests/test_clicker.py.
RECORDER_USERSCRIPT = r"""// ==UserScript==
// @name         Lilush Macro Recorder
// @namespace    lilush.bot
// @version      1.0
// @description  Запись кликов/копирований + воспроизведение + экспорт макроса для бота (F12/F9)
// @match        *://*/*
// @grant        GM_setClipboard
// @run-at       document-idle
// ==/UserScript==
(function () {
  "use strict";
  let recording = false;
  let steps = [];           // {type:'click'|'copy', label, t}
  const $ = (sel) => document.querySelector(sel);

  // ---- best human label for an element ----
  function labelFor(el) {
    if (!el) return "элемент";
    const txt = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
    if (txt && txt.length <= 60) return txt;
    return (el.getAttribute && (el.getAttribute("aria-label") || el.getAttribute("title")
      || el.getAttribute("name") || el.id)) || (txt ? txt.slice(0, 60) : el.tagName.toLowerCase());
  }

  // ---- panel UI ----
  const panel = document.createElement("div");
  panel.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;"
    + "background:#111;color:#eee;font:13px/1.4 system-ui,Arial;padding:10px 12px;"
    + "border-radius:12px;box-shadow:0 6px 24px rgba(0,0,0,.4);width:220px;opacity:.96";
  panel.innerHTML =
    '<div style="font-weight:600;margin-bottom:6px">🎬 Macro Recorder</div>'
    + '<div id="lmr-status" style="margin-bottom:8px;color:#9bd">⏹ Остановлено · 0 шаг.</div>'
    + '<button id="lmr-rec"  style="width:100%;margin:2px 0;padding:6px;border:0;border-radius:8px;cursor:pointer;background:#e33;color:#fff">⏺ Запись (F12/F9)</button>'
    + '<button id="lmr-play" style="width:100%;margin:2px 0;padding:6px;border:0;border-radius:8px;cursor:pointer;background:#39f;color:#fff">▶️ Воспроизвести</button>'
    + '<button id="lmr-copy" style="width:100%;margin:2px 0;padding:6px;border:0;border-radius:8px;cursor:pointer;background:#2a2;color:#fff">📋 Скопировать для бота</button>'
    + '<button id="lmr-clr"  style="width:100%;margin:2px 0;padding:6px;border:0;border-radius:8px;cursor:pointer;background:#555;color:#fff">🗑 Очистить</button>';
  function mount() { if (document.body) document.body.appendChild(panel); else setTimeout(mount, 300); }
  mount();

  function status() {
    const s = $("#lmr-status");
    if (s) s.textContent = (recording ? "⏺ Запись" : "⏹ Остановлено") + " · " + steps.length + " шаг.";
  }

  function toggleRec() {
    recording = !recording;
    const b = $("#lmr-rec");
    if (b) { b.textContent = recording ? "⏹ Стоп (F12/F9)" : "⏺ Запись (F12/F9)"; b.style.background = recording ? "#777" : "#e33"; }
    status();
  }

  // ---- capture clicks (capture phase so we see them first) ----
  document.addEventListener("click", function (e) {
    if (!recording) return;
    if (panel.contains(e.target)) return;   // ignore our own UI
    steps.push({ type: "click", label: labelFor(e.target), t: Date.now() });
    status();
  }, true);

  // ---- capture copies (selection + Ctrl+C) ----
  document.addEventListener("copy", function () {
    if (!recording) return;
    const sel = (window.getSelection && String(window.getSelection())) || "";
    if (sel.trim()) { steps.push({ type: "copy", label: sel.trim().slice(0, 80), t: Date.now() }); status(); }
  }, true);

  // ---- hotkeys ----
  window.addEventListener("keydown", function (e) {
    if (e.key === "F12" || e.key === "F9") { e.preventDefault(); e.stopPropagation(); toggleRec(); }
  }, true);

  // ---- export to bot macro text ----
  function toMacro() {
    const lines = [];
    let prev = null;
    for (const s of steps) {
      if (prev != null) {
        const gap = Math.round((s.t - prev) / 1000);
        if (gap >= 1) lines.push("Пауза: " + gap);
      }
      if (s.type === "copy") lines.push("Скопировать: " + s.label);
      else lines.push("Клик: " + s.label);
      prev = s.t;
    }
    return lines.join("\n");
  }

  // ---- playback (clicks only, by visible label) ----
  function findByLabel(label) {
    const els = document.querySelectorAll("a,button,input,[role=button],div,span,li");
    for (const el of els) {
      const t = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
      if (t && t === label) return el;
    }
    for (const el of els) {
      const t = (el.innerText || el.textContent || "").trim();
      if (t && t.indexOf(label) !== -1) return el;
    }
    return null;
  }
  async function play() {
    let prev = null;
    for (const s of steps) {
      if (prev != null) {
        const gap = (s.t - prev);
        await new Promise(r => setTimeout(r, Math.min(Math.max(gap, 200), 5000)));
      }
      if (s.type === "click") { const el = findByLabel(s.label); if (el) { el.style.outline = "3px solid #39f"; el.click(); setTimeout(() => el.style.outline = "", 600); } }
      prev = s.t;
    }
  }

  // ---- wire buttons ----
  panel.addEventListener("click", function (e) {
    const id = e.target && e.target.id;
    if (id === "lmr-rec") toggleRec();
    else if (id === "lmr-play") play();
    else if (id === "lmr-clr") { steps = []; status(); }
    else if (id === "lmr-copy") {
      const m = toMacro();
      try { if (typeof GM_setClipboard === "function") GM_setClipboard(m); else navigator.clipboard.writeText(m); } catch (err) {}
      alert("Макрос скопирован — отправь его боту в Telegram:\n\n" + (m || "(пусто)"));
    }
  }, true);
})();
"""

INSTRUCTIONS = (
    "<b>🌐 Рекордер действий — через сайт</b>\n\n"
    "Записывает твои клики и копирования прямо на сайте, проигрывает их и "
    "выдаёт готовый макрос для бота.\n\n"
    "<b>Как запустить:</b>\n"
    "1. Установи расширение <b>Tampermonkey</b> в браузер.\n"
    "2. Создай новый скрипт и вставь содержимое файла "
    f"<code>{RECORDER_FILENAME}</code> (ниже).\n"
    "3. Открой нужный сайт — в правом нижнем углу появится панель "
    "<b>🎬 Macro Recorder</b>.\n\n"
    "<b>Как пользоваться:</b>\n"
    "• <b>⏺ Запись</b> (горячая клавиша <b>F12</b> или <b>F9</b>) — включает запись. "
    "Скрипт запоминает, какие кнопки ты жмёшь и сколько секунд между ними.\n"
    "• <b>Копирование</b> — выдели текст и нажми Ctrl+C → шаг "
    "<code>Скопировать: …</code> добавится сам.\n"
    "• <b>▶️ Воспроизвести</b> — проиграет клики на сайте для проверки.\n"
    "• <b>📋 Скопировать для бота</b> — кладёт готовый макрос в буфер. "
    "Пришли его боту в «🤖 Макрос действий (Кликер) → ▶️ Запустить макрос»."
)
