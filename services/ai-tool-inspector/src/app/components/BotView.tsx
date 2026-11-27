import { useState, useCallback, useRef, useEffect } from 'react';
import { TelegramChat } from './TelegramChat';
import { WorkflowPanel } from './WorkflowPanel';
import { Workflow, TgMessage, AiSlot, AiTask, AiCapability, ChatMessage } from './workflowTypes';
import { triggerZipDownload } from './DownloadZip';
import { triggerFileDownload } from './FileZone';
import type { FileEntry as ZipEntry } from './TelegramChat';

let msgId = 0;
const uid = () => String(++msgId);
const now = () => new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });

function botMsg(text: string, buttons?: TgMessage['buttons']): TgMessage {
  return { id: uid(), from: 'bot', text, time: now(), buttons };
}
function userMsg(text: string): TgMessage {
  return { id: uid(), from: 'user', text, time: now() };
}
function typingMsg(): TgMessage {
  return { id: uid(), from: 'bot', text: '', time: now(), isTyping: true };
}

// ─── AI models ────────────────────────────────────────────────────────────────

const MODEL_MAP: Record<string, { name: string; provider: string }> = {
  claude_sonnet: { name: 'Claude Sonnet 4.6', provider: 'Anthropic' },
  claude_opus:   { name: 'Claude Opus 4.7',   provider: 'Anthropic' },
  gpt4o:         { name: 'GPT-4o',            provider: 'OpenAI' },
  gpt41:         { name: 'GPT-4.1',           provider: 'OpenAI' },
  gemini:        { name: 'Gemini 2.5 Pro',    provider: 'Google' },
  mistral:       { name: 'Mistral Large',     provider: 'Mistral' },
  grok:          { name: 'Grok 3',            provider: 'xAI' },
  deepseek:      { name: 'DeepSeek R2',       provider: 'DeepSeek' },
};

const DEFAULT_CAPS: AiCapability[] = [
  { id: 'bash',      label: '🔧 Bash / Терминал',        description: 'Выполнение shell-команд и скриптов',     enabled: true  },
  { id: 'files',     label: '📁 Файловая система',        description: 'Чтение, запись и поиск файлов',          enabled: true  },
  { id: 'web',       label: '🌐 Веб-запросы',             description: 'HTTP-запросы и веб-поиск',               enabled: false },
  { id: 'thinking',  label: '🧠 Расширенное мышление',    description: 'Расширенный reasoning перед ответом',    enabled: false },
  { id: 'tools',     label: '🛠 Внешние инструменты',     description: 'MCP-инструменты и API-плагины',          enabled: true  },
];

function mkCaps(): AiCapability[] {
  return DEFAULT_CAPS.map(c => ({ ...c }));
}

// Demo: slot 1 pre-connected as Claude Sonnet (self-test)
function mkSlots(): AiSlot[] {
  const demoTasks: AiTask[] = [
    { id: 'dt1', text: 'Проверить bash: echo "hello world" && ls -la', source: 'text', status: 'done',    createdAt: new Date(Date.now() - 3600000).toISOString() },
    { id: 'dt2', text: 'Проверить read: прочитать src/app/App.tsx',     source: 'text', status: 'done',    createdAt: new Date(Date.now() - 2400000).toISOString() },
    { id: 'dt3', text: 'Проверить write: создать temp/test.txt',        source: 'text', status: 'running', createdAt: new Date(Date.now() -  900000).toISOString() },
    { id: 'dt4', text: 'Проверить web-fetch: GET /api/health',          source: 'text', status: 'pending', createdAt: new Date(Date.now() -  300000).toISOString() },
    { id: 'dt5', text: 'Верифицировать все 13 инструментов Claude',     source: 'algorithm', status: 'pending', createdAt: new Date().toISOString() },
  ];
  const demoCaps = mkCaps();
  demoCaps.find(c => c.id === 'web')!.enabled = true;
  demoCaps.find(c => c.id === 'thinking')!.enabled = true;

  return Array.from({ length: 20 }, (_, i) => {
    if (i === 0) return {
      id: 1,
      connected: true,
      name: 'Claude Sonnet 4.6',
      provider: 'Anthropic',
      domain: 'localhost:3001',
      serverUrl: 'http://localhost:3001',
      modelApiId: 'claude-sonnet-4-6',
      token: 'demo_' + Math.random().toString(36).slice(2, 10),
      tasks: demoTasks,
      capabilities: demoCaps,
      chatHistory: [],
    };
    return { id: i + 1, connected: false, tasks: [], capabilities: mkCaps(), chatHistory: [] };
  });
}

// Demo workflow
function mkDemoWorkflow(): Workflow {
  return {
    id: 'wf_demo_1',
    name: 'Полная верификация Claude Sonnet 4.6',
    description: 'Автоматизированная проверка всех инструментов',
    autoContinue: true,
    currentStepIndex: 0,
    status: 'idle',
    createdAt: new Date(Date.now() - 7200000).toISOString(),
    startedAt: null,
    steps: [
      { id: 'step_d1', name: 'Bash-инструменты',    description: 'echo, ls, cat, grep, find, awk, sed',         timeLimitMinutes: 5,  modelId: 'claude-sonnet-4-6', toolIds: ['bash','grep'],       status: 'pending', startedAt: null, completedAt: null, result: null, verifiedTools: [] },
      { id: 'step_d2', name: 'Файловая система',    description: 'Read, Write, Edit, Glob, создание директорий', timeLimitMinutes: 3,  modelId: 'claude-sonnet-4-6', toolIds: ['read','write','edit'], status: 'pending', startedAt: null, completedAt: null, result: null, verifiedTools: [] },
      { id: 'step_d3', name: 'Веб и API',           description: 'HTTP-запросы, WebFetch, внешние сервисы',      timeLimitMinutes: 10, modelId: 'claude-sonnet-4-6', toolIds: ['web_fetch'],         status: 'pending', startedAt: null, completedAt: null, result: null, verifiedTools: [] },
      { id: 'step_d4', name: 'MCP-инструменты',     description: 'Supabase, Figma, GitHub MCP',                  timeLimitMinutes: 8,  modelId: 'claude-sonnet-4-6', toolIds: ['mcp'],               status: 'pending', startedAt: null, completedAt: null, result: null, verifiedTools: [] },
      { id: 'step_d5', name: 'Итоговый отчёт',      description: 'Формирование отчёта и метрик',                  timeLimitMinutes: 2,  modelId: 'claude-sonnet-4-6', toolIds: [],                    status: 'pending', startedAt: null, completedAt: null, result: null, verifiedTools: [] },
    ],
  };
}

function slotLabel(slot: AiSlot): string {
  if (!slot.connected) return `[${slot.id}] пусто`;
  const domain = (slot.domain || slot.serverUrl || '').replace(/^https?:\/\//, '').slice(0, 10);
  const name = (slot.name || 'ИИ').slice(0, 9);
  return `[${slot.id}] ${domain}…/${name}`;
}

function mkAiSlotButtons(slots: AiSlot[]): TgMessage['buttons'] {
  const rows: TgMessage['buttons'] = [];
  for (let i = 0; i < 20; i += 2) {
    rows!.push([
      { label: slotLabel(slots[i]),     action: `ai_slot_${slots[i].id}` },
      { label: slotLabel(slots[i + 1]), action: `ai_slot_${slots[i + 1].id}` },
    ]);
  }
  rows!.push([{ label: '⬅️ Меню', action: 'cmd_menu' }]);
  return rows;
}

// ─── Main menu ────────────────────────────────────────────────────────────────

const MAIN_MENU_BUTTONS: TgMessage['buttons'] = [
  [
    { label: '📊 Статистика', action: 'cmd_stats' },
    { label: '🌐 Сервисы',    action: 'cmd_services' },
  ],
  [
    { label: '🤖 ИИ (20 слотов)', action: 'cmd_ai', variant: 'primary' },
    { label: '⚙️ Алгоритмы',     action: 'cmd_workflows' },
  ],
  [
    { label: '🔗 Ссылки для ИИ', action: 'cmd_links' },
    { label: '📥 Скачать ZIP',   action: 'cmd_download_zip' },
  ],
  [{ label: '❓ Помощь', action: 'cmd_help' }],
];

const WELCOME: TgMessage[] = [
  botMsg(
    '👋 Привет! Я бот AI Tool Inspector.\n\n'
    + '🤖 Слот 1: Claude Sonnet 4.6 (5 заданий, все возможности)\n'
    + '⚙️ Алгоритм: «Полная верификация» (5 шагов, авто-продолжение)\n\n'
    + '📡 Как работает связь с ИИ:\n'
    + '1. Генерируй probe URL в Сервер → Ссылка\n'
    + '2. Дай URL ИИ-агенту (Claude Code, GPT и т.д.)\n'
    + '3. ИИ сам зайдёт, заберёт задания, выполнит\n'
    + '4. Отчитается через тот же сервер\n'
    + '💡 API-ключ НЕ нужен — ИИ приходит к тебе!',
    MAIN_MENU_BUTTONS
  ),
];

// ─── BotView ──────────────────────────────────────────────────────────────────

export function BotView({ serverUrl: serverUrlProp }: { serverUrl?: string }) {
  const [messages, setMessages]         = useState<TgMessage[]>(WELCOME);
  const [workflows, setWorkflows]       = useState<Workflow[]>(() => [mkDemoWorkflow()]);
  const [activeWorkflowId, setActiveWorkflowId] = useState<string | null>(null);
  const [stepTimers, setStepTimers]     = useState<Record<string, number>>({});
  const [aiSlots, setAiSlots]           = useState<AiSlot[]>(mkSlots);
  const [, setHasZipFile]               = useState(false);
  const [inputMode, setInputMode]       = useState<{ type: 'zadanie' | 'message' | 'direct_chat' | 'apikey'; slotId: number } | null>(null);
  const serverUrl = serverUrlProp || 'http://localhost:3001';

  const timerRefs   = useRef<Record<string, ReturnType<typeof setInterval>>>({});
  const implRef     = useRef<(action: string) => void>(() => {});
  const aiSlotsRef  = useRef(aiSlots);
  const workflowsRef = useRef(workflows);
  const inputModeRef = useRef(inputMode);

  useEffect(() => { aiSlotsRef.current  = aiSlots;    }, [aiSlots]);
  useEffect(() => { workflowsRef.current = workflows;  }, [workflows]);
  useEffect(() => { inputModeRef.current = inputMode;  }, [inputMode]);

  const activeWorkflow = workflows.find(w => w.id === activeWorkflowId) || null;

  // ── helpers ────────────────────────────────────────────────────────────────

  const push = useCallback((...msgs: TgMessage[]) => {
    setMessages(prev => [...prev.filter(m => !m.isTyping), ...msgs]);
  }, []);

  const typeAndPush = useCallback((delay: number, ...msgs: TgMessage[]) => {
    setMessages(prev => [...prev, typingMsg()]);
    setTimeout(() => push(...msgs), delay);
  }, [push]);

  const updateWorkflow = useCallback((id: string, updater: (w: Workflow) => Workflow) => {
    setWorkflows(prev => prev.map(w => w.id === id ? updater(w) : w));
  }, []);

  const startStepTimer = useCallback((stepId: string, minutes: number, onExpire: () => void) => {
    let secs = minutes * 60;
    setStepTimers(prev => ({ ...prev, [stepId]: secs }));
    timerRefs.current[stepId] = setInterval(() => {
      secs--;
      setStepTimers(prev => ({ ...prev, [stepId]: secs }));
      if (secs <= 0) {
        clearInterval(timerRefs.current[stepId]);
        onExpire();
      }
    }, 1000);
  }, []);

  const stopStepTimer = useCallback((stepId: string) => {
    clearInterval(timerRefs.current[stepId]);
    setStepTimers(prev => { const n = { ...prev }; delete n[stepId]; return n; });
  }, []);

  // ── stable dispatcher (no deps — always routes to latest impl) ────────────

  const handleAction = useCallback((action: string) => {
    implRef.current(action);
  }, []);

  // ── advance workflow ───────────────────────────────────────────────────────

  const advanceWorkflow = useCallback((wfId: string, stepResult: 'passed' | 'failed') => {
    setWorkflows(prev => {
      const wf = prev.find(w => w.id === wfId);
      if (!wf) return prev;

      const step = wf.steps[wf.currentStepIndex];
      if (step) stopStepTimer(step.id);

      const now_ = new Date().toISOString();
      const updatedSteps = wf.steps.map((s, i) =>
        i === wf.currentStepIndex
          ? { ...s, status: 'completed' as const, completedAt: now_, result: stepResult }
          : s
      );

      const nextIdx = wf.currentStepIndex + 1;
      const isDone  = nextIdx >= wf.steps.length;

      if (isDone) {
        typeAndPush(600,
          botMsg(
            `✅ Алгоритм «${wf.name}» завершён!\n\nШагов выполнено: ${wf.steps.length}\nРезультат: ${stepResult === 'passed' ? '🟢 Успешно' : '🔴 С ошибками'}`,
            [[
              { label: '📊 Итоговый отчёт', action: `report_${wfId}` },
              { label: '🔄 Запустить снова', action: `restart_${wfId}`, variant: 'primary' },
            ]]
          )
        );
        return prev.map(w => w.id === wfId
          ? { ...w, steps: updatedSteps, status: 'completed', currentStepIndex: wf.currentStepIndex }
          : w
        );
      }

      const nextStep = wf.steps[nextIdx];

      if (wf.autoContinue) {
        typeAndPush(800,
          botMsg(
            `✅ Шаг ${wf.currentStepIndex + 1} выполнен → авто-продолжение\n\n▶ Шаг ${nextIdx + 1}: ${nextStep.name}\n⏱ Лимит: ${nextStep.timeLimitMinutes} мин\n🤖 ${nextStep.modelId}`,
            nextStep.toolIds.length > 0
              ? [[
                  { label: '✅ Выполнен', action: `step_done_${wfId}_${nextIdx}`, variant: 'success' },
                  { label: '❌ Провален', action: `step_fail_${wfId}_${nextIdx}`, variant: 'danger' },
                ]]
              : [[{ label: '✅ Готово', action: `step_done_${wfId}_${nextIdx}`, variant: 'success' }]]
          )
        );
        setTimeout(() => startStepTimer(nextStep.id, nextStep.timeLimitMinutes, () => {
          handleAction(`step_timeout_${wfId}_${nextIdx}`);
        }), 900);
      } else {
        typeAndPush(800,
          botMsg(
            `⏸ Шаг ${wf.currentStepIndex + 1} выполнен.\n\nСледующий → Шаг ${nextIdx + 1}: «${nextStep.name}»\n⏱ ${nextStep.timeLimitMinutes} мин\n\nПродолжить?`,
            [[
              { label: '▶ Продолжить', action: `approve_${wfId}_${nextIdx}`, variant: 'primary' },
              { label: '⏸ Пауза',      action: `pause_${wfId}` },
            ]]
          )
        );
      }

      return prev.map(w => w.id === wfId
        ? {
            ...w,
            steps: updatedSteps.map((s, i) =>
              i === nextIdx ? { ...s, status: wf.autoContinue ? 'running' : 'pending' } : s
            ),
            currentStepIndex: nextIdx,
            status: 'running',
          }
        : w
      );
    });
  }, [stopStepTimer, typeAndPush, startStepTimer, handleAction]);

  // ── run workflow ───────────────────────────────────────────────────────────

  const handleRunWorkflow = useCallback((wfId: string, _restart = false) => {
    setWorkflows(prev => {
      const wf = prev.find(w => w.id === wfId);
      if (!wf) return prev;

      const firstStep = wf.steps[0];
      push(userMsg(`▶ Запустить: ${wf.name}`));
      typeAndPush(700,
        botMsg(
          `🚀 Алгоритм «${wf.name}» запущен!\n\nШагов: ${wf.steps.length}\nАвто: ${wf.autoContinue ? '✅ ВКЛ' : '⏸ ВЫКЛ'}\n\n▶ Шаг 1: ${firstStep.name}\n⏱ ${firstStep.timeLimitMinutes} мин\n🤖 ${firstStep.modelId}` +
          (firstStep.toolIds.length > 0 ? `\n📦 ${firstStep.toolIds.join(', ')}` : ''),
          [[
            { label: '✅ Выполнен', action: `step_done_${wfId}_0`, variant: 'success' },
            { label: '❌ Провален', action: `step_fail_${wfId}_0`, variant: 'danger' },
            { label: '⏸ Пауза',    action: `pause_${wfId}` },
          ]]
        )
      );

      setTimeout(() => startStepTimer(firstStep.id, firstStep.timeLimitMinutes, () => {
        handleAction(`step_timeout_${wfId}_0`);
      }), 800);

      const resetSteps = wf.steps.map((s, i) => ({
        ...s,
        status: i === 0 ? 'running' as const : 'pending' as const,
        startedAt: i === 0 ? new Date().toISOString() : null,
        completedAt: null,
        result: null,
        verifiedTools: [],
      }));

      return prev.map(w => w.id === wfId
        ? { ...w, steps: resetSteps, status: 'running', currentStepIndex: 0, startedAt: new Date().toISOString() }
        : w
      );
    });
  }, [push, typeAndPush, startStepTimer, handleAction]);

  // ── send handler (input bar) ───────────────────────────────────────────────

  const handleSend = useCallback(async (text: string) => {
    const mode = inputModeRef.current;
    if (!mode) return;
    const slotId = mode.slotId;

    // ── Задание → добавить в список задач слота ──────────────────────────────
    if (mode.type === 'zadanie') {
      const task: AiTask = { id: uid(), text, source: 'text', status: 'pending', createdAt: new Date().toISOString() };
      setAiSlots(prev => prev.map(s => s.id === slotId ? { ...s, tasks: [...s.tasks, task] } : s));
      push(userMsg(text));
      setInputMode(null);
      typeAndPush(400, botMsg(
        `✅ Задание добавлено:\n"${text.slice(0, 100)}${text.length > 100 ? '...' : ''}"`,
        [[
          { label: '➕ Ещё задание',  action: `ai_slot_${slotId}_zadanie_pismenno` },
          { label: '📋 Все задания',  action: `ai_slot_${slotId}_zadanie_status` },
          { label: '⬅️ Назад',        action: `ai_slot_${slotId}_zadanie` },
        ]]
      ));

    // ── Сообщение → в очередь на сервере (ИИ заберёт через probe) ────────────
    } else if (mode.type === 'message') {
      const slot = aiSlotsRef.current.find(s => s.id === slotId);
      const serverUrl = (slot?.serverUrl || 'http://localhost:3001').replace(/\/$/, '');
      const modelId   = slot?.modelApiId || `agent-${slotId}`;
      push(userMsg(text));
      setInputMode(null);
      setMessages(prev => [...prev, typingMsg()]);
      try {
        const resp = await fetch(`${serverUrl}/api/messages/${modelId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        push(botMsg(
          `✅ Сообщение отправлено в очередь!\n\nКогда ${slot?.name || 'ИИ'} зайдёт по probe-URL — заберёт его и ответит.\n\n📡 Ответ появится здесь, как только ИИ его пришлёт.`,
          [[
            { label: '✉️ Ещё написать',  action: `ai_slot_${slotId}_chat_send`, variant: 'primary' },
            { label: '📋 История',        action: `ai_slot_${slotId}_chat_history` },
            { label: '⬅️ Назад',          action: `ai_slot_${slotId}_chat` },
          ]]
        ));
      } catch {
        push(botMsg(
          `⚠️ Сервер недоступен.\nСообщение сохранено локально.\n\nЗапусти: node server.mjs`,
          [[{ label: '⬅️ Назад', action: `ai_slot_${slotId}_chat` }]]
        ));
      }

    // ── Прямой чат → сервер вызывает AI API (нужен ключ) ────────────────────
    } else if (mode.type === 'direct_chat') {
      const slot = aiSlotsRef.current.find(s => s.id === slotId);
      push(userMsg(text));
      setInputMode(null);
      const history: ChatMessage[] = slot?.chatHistory || [];
      setAiSlots(prev => prev.map(s => s.id === slotId
        ? { ...s, chatHistory: [...s.chatHistory, { role: 'user' as const, content: text }] }
        : s
      ));
      setMessages(prev => [...prev, typingMsg()]);
      const serverUrl = (slot?.serverUrl || 'http://localhost:3001').replace(/\/$/, '');
      try {
        const resp = await fetch(`${serverUrl}/api/chat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: text, history, modelId: slot?.modelApiId || 'claude-sonnet-4-6', apiKey: slot?.apiKey }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        setAiSlots(prev => prev.map(s => s.id === slotId
          ? { ...s, chatHistory: [...s.chatHistory, { role: 'assistant' as const, content: data.response }] }
          : s
        ));
        push(botMsg(data.response, [[
          { label: '💬 Продолжить', action: `ai_slot_${slotId}_direct_chat`, variant: 'primary' },
          { label: '⬅️ Назад',      action: `ai_slot_${slotId}` },
        ]]));
      } catch (err: any) {
        const msg = err.message || 'Нет связи';
        push(botMsg(
          `⚠️ ${msg}\n\n${msg.includes('API_KEY') || msg.includes('401')
            ? '🔑 Добавь API-ключ: Сервер → 🔑 API ключ'
            : '📡 Запусти server.mjs и добавь ключ в .env'
          }`,
          [[
            { label: '🔑 Ввести ключ', action: `ai_slot_${slotId}_server_apikey`, variant: 'primary' },
            { label: '⬅️ Назад',       action: `ai_slot_${slotId}` },
          ]]
        ));
      }

    // ── API ключ ─────────────────────────────────────────────────────────────
    } else if (mode.type === 'apikey') {
      const key = text.trim();
      setAiSlots(prev => prev.map(s => s.id === slotId ? { ...s, apiKey: key } : s));
      push(userMsg('••••••••' + key.slice(-4)));
      setInputMode(null);
      typeAndPush(300, botMsg(
        `✅ API-ключ сохранён для слота ${slotId}.\nТеперь «Прямой чат (API)» будет использовать его.`,
        [[
          { label: '💬 Прямой чат', action: `ai_slot_${slotId}_direct_chat`, variant: 'primary' },
          { label: '⬅️ Сервер',     action: `ai_slot_${slotId}_server` },
        ]]
      ));
    }
  }, [push, typeAndPush, setMessages]);

  // ── action implementation (redefined each render, always fresh state) ──────

  const handleActionImpl = (action: string) => {
    // ── Main menu ──────────────────────────────────────────────────────────
    if (action === 'cmd_stats') {
      push(userMsg('📊 Статистика'));
      setMessages(prev => [...prev, typingMsg()]);
      fetch(`${serverUrl}/api/summary`)
        .then(r => r.json())
        .then(data => {
          const lines = (data.summary || []).map((m: any) =>
            `${m.trustScore !== null ? (m.trustScore >= 80 ? '🟢' : m.trustScore >= 50 ? '🟡' : '🔴') : '⬜'} ${m.name}\n`
            + `  ✅${m.verified} ❌${m.failed} ⬜${m.total - m.verified - m.failed} инстр.`
            + (m.trustScore !== null ? ` | доверие: ${m.trustScore}%` : '')
          ).join('\n\n');
          push(botMsg(
            `📊 Статистика (live)\n\n${lines || 'Нет данных'}\n\n🌐 Сервер: ${data.service || 'local'} ✅`,
            [[{ label: '🔄 Обновить', action: 'cmd_stats' }, { label: '⬅️ Меню', action: 'cmd_menu' }]]
          ));
        })
        .catch(() => {
          push(botMsg(
            '📊 Статистика (offline)\n\n🟢 Claude Sonnet 4.6: ✅12 ❌1 / 13 инстр. | доверие: 92%\n⬜ GPT-4o: 7 не проверено\n⬜ Gemini 2.5: 6 не проверено\n⬜ Mistral Large: 3 не проверено\n\n⚠️ Сервер не запущен — показаны кешированные данные\nЗапусти: node server.mjs',
            [[{ label: '🔄 Обновить', action: 'cmd_stats' }, { label: '⬅️ Меню', action: 'cmd_menu' }]]
          ));
        });
    }

    else if (action === 'cmd_services') {
      push(userMsg('🌐 Сервисы'));
      setMessages(prev => [...prev, typingMsg()]);
      fetch(`${serverUrl}/api/health`)
        .then(r => r.json())
        .then(data => {
          push(botMsg(
            `🌐 Серверы\n\n🟢 ${data.service || 'local'} — ${data.url || 'http://localhost:3001'}\n`
            + `   ⬆️ Аптайм: ${Math.floor((data.uptime || 0) / 60)} мин\n`
            + `   🤖 Моделей: ${data.models} | 🔧 Инструментов: ${data.totalTools}\n\n`
            + `📡 Swagger UI: ${data.url || 'http://localhost:3001'}/docs`,
            [[{ label: '🔄 Обновить', action: 'cmd_services' }, { label: '⬅️ Меню', action: 'cmd_menu' }]]
          ));
        })
        .catch(() => push(botMsg(
          '🌐 Серверы\n\n⚠️ localhost:3001 — OFFLINE\n\nЗапусти сервер:\n  node server.mjs\n\nДеплой на Render: render.yaml уже настроен',
          [[{ label: '🔄 Обновить', action: 'cmd_services' }, { label: '⬅️ Меню', action: 'cmd_menu' }]]
        )));
    }

    else if (action === 'cmd_menu' || action === 'main_menu') {
      push(userMsg('⬅️ Меню'));
      typeAndPush(400, botMsg('Главное меню:', MAIN_MENU_BUTTONS));
    }

    else if (action === 'cmd_help') {
      push(userMsg('❓ Помощь'));
      typeAndPush(500,
        botMsg(
          '📖 Справка\n\n🤖 ИИ — 20 слотов для подключения агентов.\nКаждый слот: задания, чат, управление сервером.\n\n⚙️ Алгоритмы — последовательность шагов проверки.\nАвто-продолжение: бот сам переходит к след. шагу.\n\n📥 Скачать ZIP — получить весь проект.\n\n🔗 Ссылки — персональные токены для ИИ-агентов.',
          [[{ label: '⬅️ Меню', action: 'cmd_menu' }]]
        )
      );
    }

    else if (action === 'cmd_workflows') {
      push(userMsg('⚙️ Алгоритмы'));
      const wfs = workflowsRef.current;
      typeAndPush(500,
        botMsg(
          wfs.length > 0
            ? `⚙️ Твои алгоритмы (${wfs.length}):\n` + wfs.map((w, i) => `${i + 1}. ${w.name} — ${w.status}`).join('\n')
            : '⚙️ Алгоритмов пока нет.\nСоздай первый в панели справа →',
          [[
            { label: '➕ Создать алгоритм', action: 'create_workflow_hint', variant: 'primary' },
            { label: '⬅️ Меню',             action: 'cmd_menu' },
          ]]
        )
      );
    }

    else if (action === 'cmd_links') {
      push(userMsg('🔗 Ссылки для ИИ'));
      typeAndPush(600,
        botMsg(
          '🔗 Выбери ИИ для генерации персональной ссылки:',
          [
            [{ label: 'Claude Sonnet 4.6', action: 'gen_link_claude-sonnet-4-6' }],
            [{ label: 'GPT-4o',            action: 'gen_link_gpt-4o' }],
            [{ label: 'Gemini 2.5 Pro',    action: 'gen_link_gemini-2-5-pro' }],
            [{ label: 'Mistral Large',     action: 'gen_link_mistral-large' }],
            [{ label: '⬅️ Меню',           action: 'cmd_menu' }],
          ]
        )
      );
    }

    else if (action === 'cmd_download_zip') {
      push(userMsg('📥 Скачать ZIP'));
      typeAndPush(400, botMsg('⏳ Создаю ZIP-архив...'));
      triggerZipDownload()
        .then(() => {
          push(botMsg('✅ ZIP-архив скачан!\n\nВ архиве: весь проект (сервер, бот, фронтенд, Docker, конфиги).', [[{ label: '⬅️ Меню', action: 'cmd_menu' }]]));
        })
        .catch(() => {
          push(botMsg('❌ Ошибка при создании архива.', [[{ label: '🔄 Попробовать снова', action: 'cmd_download_zip' }, { label: '⬅️ Меню', action: 'cmd_menu' }]]));
        });
    }

    // ── Link generation ────────────────────────────────────────────────────
    else if (action.startsWith('gen_link_')) {
      const modelId = action.replace('gen_link_', '');
      const names: Record<string, string> = {
        'claude-sonnet-4-6': 'Claude Sonnet 4.6',
        'gpt-4o':            'GPT-4o',
        'gemini-2-5-pro':    'Gemini 2.5 Pro',
        'mistral-large':     'Mistral Large',
      };
      push(userMsg(names[modelId] || modelId));
      const token = Math.random().toString(36).slice(2, 18);
      typeAndPush(800,
        botMsg(
          `🔗 Ссылка для ${names[modelId] || modelId}\n\nТокен: ${token}\n\n📥 GET http://localhost:3001/api/probe/${modelId}?token=${token}\n\n📤 POST http://localhost:3001/api/probe/${modelId}/verify?token=${token}\n\nТело: {"toolId":"bash","result":"verified","message":"..."}`,
          [[
            { label: '🔄 Перегенерировать', action: `gen_link_${modelId}` },
            { label: '⬅️ Назад',            action: 'cmd_links' },
          ]]
        )
      );
    }

    // ── AI Slots ───────────────────────────────────────────────────────────
    else if (action === 'cmd_ai') {
      push(userMsg('🤖 ИИ'));
      typeAndPush(500, botMsg('🤖 Слоты ИИ-агентов (20):\nВыбери слот для управления:', mkAiSlotButtons(aiSlotsRef.current)));
    }

    else if (/^ai_slot_(\d+)$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      if (!slot) return;
      push(userMsg(`[${slotId}]`));
      const serverLabel = slot.connected ? '🟢 Сервер' : '⚪ Сервер';
      const info = slot.connected
        ? `🤖 Слот ${slotId}: ${slot.name}\n🏢 ${slot.provider}\n🌐 ${slot.domain || slot.serverUrl}\n📋 Заданий: ${slot.tasks.length}`
        : `🔲 Слот ${slotId}: пусто`;
      const capsEnabled = slot.capabilities?.filter(c => c.enabled).length ?? 0;
      const capsTotal   = slot.capabilities?.length ?? 0;
      typeAndPush(400,
        botMsg(info, [
          [
            { label: '📋 Задание',      action: `ai_slot_${slotId}_zadanie` },
            { label: '💬 Чат с ИИ',    action: `ai_slot_${slotId}_chat` },
          ],
          [
            { label: serverLabel,        action: `ai_slot_${slotId}_server` },
            { label: '🔍 Полный обзор',  action: `ai_slot_${slotId}_overview` },
          ],
          [
            { label: `🧩 Возможности (${capsEnabled}/${capsTotal})`, action: `ai_slot_${slotId}_caps` },
            { label: '⚡ Быстрый тест',                              action: `ai_slot_${slotId}_quicktest` },
          ],
          [{ label: '⬅️ Назад', action: 'cmd_ai' }],
        ])
      );
    }

    else if (/^ai_slot_(\d+)_connect_(\w+)$/.test(action)) {
      const m       = action.match(/^ai_slot_(\d+)_connect_(\w+)$/);
      const slotId  = Number(m![1]);
      const modelKey = m![2];
      const model   = MODEL_MAP[modelKey];
      if (!model) return;
      push(userMsg(model.name));
      const MODEL_API_IDS: Record<string, string> = {
        claude_sonnet: 'claude-sonnet-4-6', claude_opus: 'claude-opus-4-7',
        gpt4o: 'gpt-4o', gpt41: 'gpt-4.1',
        gemini: 'gemini-2.5-pro-preview-06-05', mistral: 'mistral-large-latest',
        grok: 'grok-3', deepseek: 'deepseek-chat',
      };
      const sUrl = serverUrl.replace(/\/$/, '');
      const domain = sUrl.replace(/^https?:\/\//, '').split('/')[0];
      setAiSlots(prev => prev.map(s => s.id === slotId
        ? { ...s, connected: true, name: model.name, provider: model.provider, domain, serverUrl: sUrl, modelApiId: MODEL_API_IDS[modelKey] || modelKey, capabilities: s.capabilities?.length ? s.capabilities : mkCaps() }
        : s
      ));
      typeAndPush(600,
        botMsg(
          `✅ Слот ${slotId} подключён!\n\n🤖 ${model.name}\n🏢 ${model.provider}\n🌐 localhost:3001`,
          [
            [
              { label: '📋 Задание', action: `ai_slot_${slotId}_zadanie` },
              { label: '💬 Чат с ИИ', action: `ai_slot_${slotId}_chat` },
            ],
            [{ label: '⬅️ К слотам', action: 'cmd_ai' }],
          ]
        )
      );
    }

    // ── Задание sub-menu ───────────────────────────────────────────────────
    else if (/^ai_slot_(\d+)_zadanie$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_zadanie$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      push(userMsg('📋 Задание'));
      typeAndPush(400,
        botMsg(
          `📋 Задания — Слот ${slotId} (${slot?.name || 'не подключён'})\nВсего заданий: ${slot?.tasks.length ?? 0}`,
          [
            [
              { label: '➕ Добавить',        action: `ai_slot_${slotId}_zadanie_add` },
              { label: '✍️ Письменно',       action: `ai_slot_${slotId}_zadanie_pismenno` },
            ],
            [
              { label: '📤 Экспорт',         action: `ai_slot_${slotId}_zadanie_export` },
              { label: '📥 Импорт',          action: `ai_slot_${slotId}_zadanie_import` },
            ],
            [
              { label: '📊 Статус',          action: `ai_slot_${slotId}_zadanie_status` },
              { label: '⚙️ Алгоритм',        action: `ai_slot_${slotId}_zadanie_algo` },
            ],
            [{ label: '⬅️ Назад',            action: `ai_slot_${slotId}` }],
          ]
        )
      );
    }

    else if (/^ai_slot_(\d+)_zadanie_add$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_zadanie_add$/)?.[1]);
      setInputMode({ type: 'zadanie', slotId });
      push(userMsg('➕ Добавить'));
      typeAndPush(300, botMsg(`✍️ Введи задание для слота ${slotId} в поле ввода ниже:`));
    }

    else if (/^ai_slot_(\d+)_zadanie_pismenno$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_zadanie_pismenno$/)?.[1]);
      setInputMode({ type: 'zadanie', slotId });
      push(userMsg('✍️ Письменно'));
      typeAndPush(300, botMsg(`📝 Напиши задание для слота ${slotId} в поле ввода ниже:`));
    }

    else if (/^ai_slot_(\d+)_zadanie_export$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_zadanie_export$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      push(userMsg('📤 Экспорт'));
      if (!slot?.tasks.length) {
        typeAndPush(400, botMsg('📭 Заданий нет — нечего экспортировать.', [[{ label: '⬅️ Назад', action: `ai_slot_${slotId}_zadanie` }]]));
      } else {
        const text = slot.tasks.map((t, i) => `${i + 1}. [${t.status}] ${t.text}`).join('\n');
        typeAndPush(400,
          botMsg(
            `📤 Задания слота ${slotId} (${slot.tasks.length}):\n\n${text}`,
            [[
              { label: '⬅️ Назад', action: `ai_slot_${slotId}_zadanie` },
              { label: '🔄 Обновить', action: `ai_slot_${slotId}_zadanie_export` },
            ]]
          )
        );
      }
    }

    else if (/^ai_slot_(\d+)_zadanie_import$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_zadanie_import$/)?.[1]);
      push(userMsg('📥 Импорт'));
      typeAndPush(400,
        botMsg(
          '📥 Выбери формат импорта:',
          [
            [
              { label: 'TXT',  action: `ai_slot_${slotId}_zadanie_import_txt` },
              { label: 'JSON', action: `ai_slot_${slotId}_zadanie_import_json` },
              { label: 'CSV',  action: `ai_slot_${slotId}_zadanie_import_csv` },
            ],
            [
              { label: 'MD',   action: `ai_slot_${slotId}_zadanie_import_md` },
              { label: 'YAML', action: `ai_slot_${slotId}_zadanie_import_yaml` },
            ],
            [{ label: '⬅️ Назад', action: `ai_slot_${slotId}_zadanie` }],
          ]
        )
      );
    }

    else if (/^ai_slot_(\d+)_zadanie_import_\w+$/.test(action)) {
      const m      = action.match(/^ai_slot_(\d+)_zadanie_import_(\w+)$/);
      const slotId = Number(m![1]);
      const fmt    = m![2].toUpperCase();
      setInputMode({ type: 'zadanie', slotId });
      push(userMsg(fmt));
      typeAndPush(300, botMsg(`📥 Вставь задания в формате ${fmt} в поле ввода ниже (одно задание на строку):`));
    }

    else if (/^ai_slot_(\d+)_zadanie_status$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_zadanie_status$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      push(userMsg('📊 Статус'));
      if (!slot?.tasks.length) {
        typeAndPush(400, botMsg('📭 Заданий нет.', [[{ label: '➕ Добавить', action: `ai_slot_${slotId}_zadanie_pismenno`, variant: 'primary' }, { label: '⬅️ Назад', action: `ai_slot_${slotId}_zadanie` }]]));
      } else {
        const pending = slot.tasks.filter(t => t.status === 'pending').length;
        const running = slot.tasks.filter(t => t.status === 'running').length;
        const done    = slot.tasks.filter(t => t.status === 'done').length;
        const list    = slot.tasks.slice(-10).map((t, i) => {
          const icon = t.status === 'done' ? '✅' : t.status === 'running' ? '🔄' : '⏳';
          return `${icon} ${i + 1}. ${t.text.slice(0, 60)}${t.text.length > 60 ? '...' : ''}`;
        }).join('\n');
        typeAndPush(500,
          botMsg(
            `📊 Статус задания — Слот ${slotId} (${slot.name})\n\n⏳ Ожидает: ${pending}\n🔄 В работе: ${running}\n✅ Готово: ${done}\n\nПоследние ${Math.min(slot.tasks.length, 10)}:\n${list}`,
            [[
              { label: '▶ Запустить все', action: `ai_slot_${slotId}_zadanie_runall`, variant: 'primary' },
              { label: '⬅️ Назад',        action: `ai_slot_${slotId}_zadanie` },
            ]]
          )
        );
      }
    }

    else if (/^ai_slot_(\d+)_zadanie_runall$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_zadanie_runall$/)?.[1]);
      push(userMsg('▶ Запустить все'));
      setAiSlots(prev => prev.map(s => s.id === slotId
        ? { ...s, tasks: s.tasks.map(t => ({ ...t, status: 'running' as const })) }
        : s
      ));
      typeAndPush(600, botMsg(`🚀 Все задания слота ${slotId} запущены!`, [[{ label: '📊 Статус', action: `ai_slot_${slotId}_zadanie_status` }]]));
    }

    else if (/^ai_slot_(\d+)_zadanie_algo$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_zadanie_algo$/)?.[1]);
      const wfs    = workflowsRef.current;
      push(userMsg('⚙️ Алгоритм'));
      typeAndPush(500,
        botMsg(
          wfs.length > 0
            ? `⚙️ Выбери алгоритм для слота ${slotId}:\n` + wfs.map((w, i) => `${i + 1}. ${w.name}`).join('\n')
            : '⚙️ Алгоритмов нет. Создай в панели справа →',
          [[{ label: '⬅️ Назад', action: `ai_slot_${slotId}_zadanie` }]]
        )
      );
    }

    // ── Канал связи (probe-based, API не нужен) ────────────────────────────
    else if (/^ai_slot_(\d+)_chat$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_chat$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      push(userMsg('💬 Чат с ИИ'));
      const serverUrl = slot?.serverUrl || 'http://localhost:3001';
      const modelId   = slot?.modelApiId || `agent-${slotId}`;
      const token     = slot?.token || '(сгенерируй в Сервер → Ссылка)';
      typeAndPush(400, botMsg(
        `📡 Канал связи — Слот ${slotId}${slot?.name ? ` (${slot.name})` : ''}\n\n`
        + `🔗 Probe URL для ИИ-агента:\n${serverUrl}/api/probe/${modelId}?token=${token}\n\n`
        + `✅ Как это работает:\n`
        + `1. Дай ИИ этот URL\n`
        + `2. ИИ зайдёт и возьмёт задания\n`
        + `3. Выполнит их и ответит сюда\n\n`
        + `💡 API-ключ НЕ нужен — ИИ сам приходит к тебе!`,
        [
          [
            { label: '✉️ Написать ИИ',       action: `ai_slot_${slotId}_chat_send`, variant: 'primary' },
            { label: '📋 История сообщений', action: `ai_slot_${slotId}_chat_history` },
          ],
          [
            { label: '🔗 Сгенерировать URL', action: `ai_slot_${slotId}_server_genlink` },
            { label: '🔬 Прямой чат (API)',  action: `ai_slot_${slotId}_direct_chat` },
          ],
          [{ label: '⬅️ Назад', action: `ai_slot_${slotId}` }],
        ]
      ));
    }

    else if (/^ai_slot_(\d+)_chat_send$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_chat_send$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      setInputMode({ type: 'message', slotId });
      push(userMsg('✉️ Написать ИИ'));
      typeAndPush(300, botMsg(
        `✉️ Напиши сообщение для ${slot?.name || `слота ${slotId}`}:\n\nОно попадёт в очередь на сервере. ИИ заберёт его при следующем обращении к probe URL.`
      ));
    }

    else if (/^ai_slot_(\d+)_chat_history$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_chat_history$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      push(userMsg('📋 История'));
      const serverUrl = (slot?.serverUrl || 'http://localhost:3001').replace(/\/$/, '');
      const modelId   = slot?.modelApiId || `agent-${slotId}`;
      setMessages(prev => [...prev, typingMsg()]);
      fetch(`${serverUrl}/api/messages/${modelId}`)
        .then(r => r.json())
        .then(data => {
          const msgs = data.messages || [];
          if (!msgs.length) {
            push(botMsg('📭 Сообщений пока нет.\nОтправь первое сообщение → ИИ ответит через probe.', [[{ label: '✉️ Написать', action: `ai_slot_${slotId}_chat_send`, variant: 'primary' }, { label: '⬅️ Назад', action: `ai_slot_${slotId}_chat` }]]));
          } else {
            const text = msgs.slice(-10).map((m: any) =>
              `👤 Ты: ${m.text.slice(0, 80)}\n${m.reply ? `🤖 ИИ: ${m.reply.slice(0, 80)}` : '⏳ Ожидаем ответа...'}`
            ).join('\n\n');
            push(botMsg(`📋 История — Слот ${slotId}\n\n${text}`, [[{ label: '✉️ Написать', action: `ai_slot_${slotId}_chat_send`, variant: 'primary' }, { label: '⬅️ Назад', action: `ai_slot_${slotId}_chat` }]]));
          }
        })
        .catch(() => push(botMsg('⚠️ Сервер недоступен. Запусти: node server.mjs', [[{ label: '⬅️ Назад', action: `ai_slot_${slotId}_chat` }]])));
    }

    else if (/^ai_slot_(\d+)_direct_chat$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_direct_chat$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      setInputMode({ type: 'direct_chat', slotId });
      push(userMsg('🔬 Прямой чат (API)'));
      const hasKey = !!slot?.apiKey || false;
      typeAndPush(300, botMsg(
        `🔬 Прямой чат с ${slot?.name || `слот ${slotId}`}\n\n`
        + `${hasKey ? `✅ API-ключ: ••••${slot?.apiKey?.slice(-4)}` : '⚠️ API-ключ не установлен → Сервер → 🔑 API ключ'}\n\n`
        + `Это режим, где сервер напрямую обращается к AI API.\nВ отличие от probe-канала, здесь нужен ключ.`,
        [[
          { label: '🔑 Ввести API-ключ', action: `ai_slot_${slotId}_server_apikey` },
          { label: '⬅️ Назад',           action: `ai_slot_${slotId}_chat` },
        ]]
      ));
    }

    // ── Server sub-menu ────────────────────────────────────────────────────
    else if (/^ai_slot_(\d+)_server$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_server$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      push(userMsg('🖥 Сервер'));
      const connected = slot?.connected;
      const probeUrl = connected && slot?.token
        ? `${slot.serverUrl}/api/probe/${slot.modelApiId}?token=${slot.token}`
        : null;
      typeAndPush(400,
        botMsg(
          connected
            ? `🟢 Сервер — Слот ${slotId}\n🌐 ${slot?.serverUrl}\n🤖 ${slot?.name}\n\n`
              + (probeUrl ? `📡 Probe URL:\n${probeUrl}` : `📡 Сгенерируй токен для probe URL`)
            : `⚪ Сервер — Слот ${slotId}\n\nНе подключён. Нажми «Подключить» чтобы выбрать модель.`,
          [
            connected
              ? [
                  { label: '🔗 Сгенерировать URL', action: `ai_slot_${slotId}_server_genlink`, variant: 'primary' as const },
                  { label: '🔑 API ключ (прямой чат)', action: `ai_slot_${slotId}_server_apikey` },
                ]
              : [{ label: '🔌 Подключить ИИ', action: `ai_slot_${slotId}_server_connect`, variant: 'primary' as const }],
            connected
              ? [{ label: '🗑 Отключить', action: `ai_slot_${slotId}_server_delete`, variant: 'danger' as const }]
              : [],
            [{ label: '⬅️ Назад', action: `ai_slot_${slotId}` }],
          ].filter(row => row.length > 0)
        )
      );
    }

    else if (/^ai_slot_(\d+)_server_connect$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_server_connect$/)?.[1]);
      push(userMsg('🔌 Подключить ИИ'));
      typeAndPush(400,
        botMsg(
          `Выбери модель для слота ${slotId}:`,
          [
            [
              { label: 'Claude Sonnet 4.6', action: `ai_slot_${slotId}_connect_claude_sonnet` },
              { label: 'Claude Opus 4.7',   action: `ai_slot_${slotId}_connect_claude_opus` },
            ],
            [
              { label: 'GPT-4o',  action: `ai_slot_${slotId}_connect_gpt4o` },
              { label: 'GPT-4.1', action: `ai_slot_${slotId}_connect_gpt41` },
            ],
            [
              { label: 'Gemini 2.5 Pro', action: `ai_slot_${slotId}_connect_gemini` },
              { label: 'Mistral Large',  action: `ai_slot_${slotId}_connect_mistral` },
            ],
            [
              { label: 'Grok 3',      action: `ai_slot_${slotId}_connect_grok` },
              { label: 'DeepSeek R2', action: `ai_slot_${slotId}_connect_deepseek` },
            ],
            [{ label: '⬅️ Назад', action: `ai_slot_${slotId}_server` }],
          ]
        )
      );
    }

    else if (/^ai_slot_(\d+)_server_apikey$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_server_apikey$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      setInputMode({ type: 'apikey', slotId });
      push(userMsg('🔑 API ключ'));
      typeAndPush(300, botMsg(
        `🔑 Введи API-ключ для слота ${slotId} (${slot?.name || 'ИИ'}):\n\n${slot?.apiKey ? `✅ Текущий ключ: ••••${slot.apiKey.slice(-6)}` : '⚠️ Ключ не установлен'}\n\nКлюч хранится только в браузере и передаётся на твой сервер (localhost:3001).`
      ));
    }

    else if (/^ai_slot_(\d+)_server_genlink$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_server_genlink$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      push(userMsg('🔗 Сгенерировать URL'));
      const serverUrl = (slot?.serverUrl || 'http://localhost:3001').replace(/\/$/, '');
      const modelId   = slot?.modelApiId || `claude-sonnet-4-6`;
      setMessages(prev => [...prev, typingMsg()]);
      fetch(`${serverUrl}/api/tokens/${modelId}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
          if (!data.token) throw new Error(data.error || 'Нет токена');
          setAiSlots(prev => prev.map(s => s.id === slotId ? { ...s, token: data.token } : s));
          push(botMsg(
            `🔗 Probe URL для ${slot?.name || modelId}\n\n`
            + `📥 GET (задания):\n${data.probeUrl}\n\n`
            + `📤 POST (результат верификации):\n${data.verifyUrl}\n\n`
            + `📤 POST (ответ на сообщение):\n${serverUrl}/api/probe/${modelId}/reply?token=${data.token}\n\n`
            + `💡 Дай ИИ GET-ссылку — он сам всё сделает без API-ключа!`,
            [[
              { label: '📡 Канал связи',  action: `ai_slot_${slotId}_chat`, variant: 'primary' },
              { label: '🔄 Новый URL',    action: `ai_slot_${slotId}_server_genlink` },
              { label: '⬅️ Назад',        action: `ai_slot_${slotId}_server` },
            ]]
          ));
        })
        .catch(() => {
          const token = Math.random().toString(36).slice(2, 18);
          setAiSlots(prev => prev.map(s => s.id === slotId ? { ...s, token } : s));
          push(botMsg(
            `🔗 URL сгенерирован локально (сервер offline)\n\n`
            + `📥 ${serverUrl}/api/probe/${modelId}?token=${token}\n\n`
            + `⚠️ Запусти server.mjs для сохранения токена на сервере`,
            [[{ label: '⬅️ Назад', action: `ai_slot_${slotId}_server` }]]
          ));
        });
    }

    else if (/^ai_slot_(\d+)_server_delete$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_server_delete$/)?.[1]);
      push(userMsg('🗑 Отключить'));
      setAiSlots(prev => prev.map(s => s.id === slotId
        ? { id: s.id, connected: false, tasks: s.tasks, capabilities: s.capabilities, chatHistory: s.chatHistory }
        : s
      ));
      setInputMode(prev => (prev?.slotId === slotId ? null : prev));
      typeAndPush(400,
        botMsg(
          `🗑 Слот ${slotId} сброшен. ИИ-агент отключён.`,
          [[
            { label: '🔌 Подключить снова', action: `ai_slot_${slotId}_server_connect`, variant: 'primary' },
            { label: '⬅️ К слотам',         action: 'cmd_ai' },
          ]]
        )
      );
    }

    // ── Full overview ──────────────────────────────────────────────────────
    else if (/^ai_slot_(\d+)_overview$/.test(action)) {
      push(userMsg('🔍 Полный обзор'));
      const slots = aiSlotsRef.current;
      const connected = slots.filter(s => s.connected);
      const lines = connected.length > 0
        ? connected.map(s => {
            const on = s.capabilities?.filter(c => c.enabled).length ?? 0;
            const total = s.capabilities?.length ?? 0;
            const taskDone = s.tasks.filter(t => t.status === 'done').length;
            return `🟢 [${s.id}] ${s.name} (${s.provider})\n    🌐 ${s.domain} | 📋 ${s.tasks.length} заданий (✅${taskDone}) | 🧩 ${on}/${total} возможн.`;
          }).join('\n\n')
        : '⚪️ Нет подключённых агентов';
      typeAndPush(500,
        botMsg(
          `🔍 Обзор всех ИИ-агентов\n\nПодключено: ${connected.length}/20\n\n${lines}`,
          [[
            { label: '🤖 К слотам', action: 'cmd_ai' },
            { label: '⬅️ Меню',     action: 'cmd_menu' },
          ]]
        )
      );
    }

    // ── Capabilities ──────────────────────────────────────────────────────
    else if (/^ai_slot_(\d+)_caps$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_caps$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      push(userMsg('🧩 Возможности'));
      const caps = slot?.capabilities ?? mkCaps();
      const list = caps.map(c => `${c.enabled ? '✅' : '❌'} ${c.label}\n     ${c.description}`).join('\n');
      const on = caps.filter(c => c.enabled).length;
      typeAndPush(400,
        botMsg(
          `🧩 Возможности ИИ — Слот ${slotId}${slot?.name ? ` (${slot.name})` : ''}\n\n${list}\n\n✅ Включено: ${on}/${caps.length}`,
          [
            [
              { label: '🟢 Включить все',        action: `ai_slot_${slotId}_caps_enable_all`,  variant: 'success' },
              { label: '🔴 Выключить все',        action: `ai_slot_${slotId}_caps_disable_all`, variant: 'danger' },
            ],
            [
              { label: '📋 Вывод возможностей',   action: `ai_slot_${slotId}_caps_list` },
              { label: '🔧 Настроить отдельно',   action: `ai_slot_${slotId}_caps_each` },
            ],
            [{ label: '⬅️ Назад', action: `ai_slot_${slotId}` }],
          ]
        )
      );
    }

    else if (/^ai_slot_(\d+)_caps_enable_all$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_caps_enable_all$/)?.[1]);
      setAiSlots(prev => prev.map(s => s.id === slotId
        ? { ...s, capabilities: s.capabilities.map(c => ({ ...c, enabled: true })) }
        : s
      ));
      push(userMsg('🟢 Включить все'));
      typeAndPush(300,
        botMsg(`✅ Все возможности слота ${slotId} включены!`, [
          [{ label: '🧩 Возможности', action: `ai_slot_${slotId}_caps` }, { label: '⬅️ Назад', action: `ai_slot_${slotId}` }],
        ])
      );
    }

    else if (/^ai_slot_(\d+)_caps_disable_all$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_caps_disable_all$/)?.[1]);
      setAiSlots(prev => prev.map(s => s.id === slotId
        ? { ...s, capabilities: s.capabilities.map(c => ({ ...c, enabled: false })) }
        : s
      ));
      push(userMsg('🔴 Выключить все'));
      typeAndPush(300,
        botMsg(`🔴 Все возможности слота ${slotId} выключены.`, [
          [{ label: '🟢 Включить все', action: `ai_slot_${slotId}_caps_enable_all`, variant: 'success' }, { label: '⬅️ Назад', action: `ai_slot_${slotId}` }],
        ])
      );
    }

    else if (/^ai_slot_(\d+)_caps_list$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_caps_list$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      push(userMsg('📋 Вывод возможностей'));
      const caps = slot?.capabilities ?? mkCaps();
      const enabled  = caps.filter(c => c.enabled);
      const disabled = caps.filter(c => !c.enabled);
      const text = `📋 Возможности слота ${slotId}${slot?.name ? ` — ${slot.name}` : ''}\n\n`
        + `🟢 Включено (${enabled.length}):\n${enabled.map(c => `  • ${c.label}`).join('\n') || '  нет'}\n\n`
        + `🔴 Выключено (${disabled.length}):\n${disabled.map(c => `  • ${c.label}`).join('\n') || '  нет'}\n\n`
        + `📌 Все возможности для работы ИИ-агента через этот бот:\n`
        + caps.map(c => `  ${c.enabled ? '✅' : '⬜'} ${c.label} — ${c.description}`).join('\n');
      typeAndPush(500,
        botMsg(text, [[
          { label: '🧩 Управлять', action: `ai_slot_${slotId}_caps` },
          { label: '⬅️ Назад',    action: `ai_slot_${slotId}` },
        ]])
      );
    }

    else if (/^ai_slot_(\d+)_caps_each$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_caps_each$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      push(userMsg('🔧 Настроить отдельно'));
      const caps = slot?.capabilities ?? mkCaps();
      typeAndPush(400,
        botMsg(
          `🔧 Выбери возможность для переключения:`,
          [
            ...caps.map(c => ([{
              label: `${c.enabled ? '✅' : '⬜'} ${c.label}`,
              action: `ai_slot_${slotId}_caps_toggle_${c.id}`,
              variant: (c.enabled ? 'success' : 'default') as 'success' | 'default',
            }])),
            [{ label: '⬅️ Назад', action: `ai_slot_${slotId}_caps` }],
          ]
        )
      );
    }

    else if (/^ai_slot_(\d+)_caps_toggle_(\w+)$/.test(action)) {
      const m      = action.match(/^ai_slot_(\d+)_caps_toggle_(\w+)$/);
      const slotId = Number(m![1]);
      const capId  = m![2];
      let newEnabled = false;
      setAiSlots(prev => prev.map(s => {
        if (s.id !== slotId) return s;
        const updated = s.capabilities.map(c => {
          if (c.id !== capId) return c;
          newEnabled = !c.enabled;
          return { ...c, enabled: !c.enabled };
        });
        return { ...s, capabilities: updated };
      }));
      const slot = aiSlotsRef.current.find(s => s.id === slotId);
      const cap  = slot?.capabilities.find(c => c.id === capId);
      push(userMsg(cap?.label || capId));
      typeAndPush(300,
        botMsg(
          `${newEnabled ? '✅ Включено' : '🔴 Выключено'}: ${cap?.label || capId}`,
          [[{ label: '🔧 К списку', action: `ai_slot_${slotId}_caps_each` }, { label: '⬅️ Назад', action: `ai_slot_${slotId}_caps` }]]
        )
      );
    }

    // ── Quick test — реальный ping к серверу ──────────────────────────────
    else if (/^ai_slot_(\d+)_quicktest$/.test(action)) {
      const slotId = Number(action.match(/^ai_slot_(\d+)_quicktest$/)?.[1]);
      const slot   = aiSlotsRef.current.find(s => s.id === slotId);
      push(userMsg('⚡ Быстрый тест'));
      if (!slot?.connected) {
        typeAndPush(400, botMsg(
          `⚠️ Слот ${slotId} не подключён. Сначала подключи модель через Сервер.`,
          [[{ label: '⚪ Сервер', action: `ai_slot_${slotId}_server` }, { label: '⬅️ Назад', action: `ai_slot_${slotId}` }]]
        ));
        return;
      }
      const serverUrl = (slot.serverUrl || 'http://localhost:3001').replace(/\/$/, '');
      setMessages(prev => [...prev, { ...typingMsg(), text: `⏳ Проверяю ${serverUrl}...` }]);
      const t0 = Date.now();
      fetch(`${serverUrl}/api/health`)
        .then(r => r.json())
        .then(data => {
          const latency = Date.now() - t0;
          const modelStats = data.stats?.find((s: any) => s.id === slot.modelApiId);
          push(botMsg(
            `⚡ Тест — Слот ${slotId} ✅\n\n🤖 ${slot.name} (${slot.provider})\n🌐 ${serverUrl}\n⏱ Пинг: ${latency}ms\n⬆️ Аптайм: ${Math.floor((data.uptime || 0) / 60)} мин\n\n`
            + (modelStats
              ? `📊 Верификация ${slot.name}:\n✅ Подтверждено: ${modelStats.verified}\n❌ Не существует: ${modelStats.failed}\n⬜ Не проверено: ${modelStats.total - modelStats.verified - modelStats.failed}`
              : `📊 Моделей на сервере: ${data.models}\n🔧 Инструментов всего: ${data.totalTools}`)
            + `\n\n🧩 Включено возможностей: ${slot.capabilities?.filter(c => c.enabled).length}/${slot.capabilities?.length}\n`
            + `📋 Задания: ⏳${slot.tasks.filter(t => t.status === 'pending').length} 🔄${slot.tasks.filter(t => t.status === 'running').length} ✅${slot.tasks.filter(t => t.status === 'done').length}`,
            [[
              { label: '📡 Написать ИИ', action: `ai_slot_${slotId}_chat_send`, variant: 'primary' },
              { label: '📋 Задания',      action: `ai_slot_${slotId}_zadanie` },
              { label: '⬅️ Назад',        action: `ai_slot_${slotId}` },
            ]]
          ));
        })
        .catch(() => {
          push(botMsg(
            `❌ Сервер недоступен: ${serverUrl}\n\nЗапусти: node server.mjs`,
            [[{ label: '⬅️ Назад', action: `ai_slot_${slotId}` }]]
          ));
        });
    }

    // ── Workflow actions ───────────────────────────────────────────────────
    else if (action.startsWith('run_workflow_')) {
      handleRunWorkflow(action.replace('run_workflow_', ''));
    }

    else if (action.startsWith('step_done_')) {
      const parts = action.split('_');
      const wfId  = parts[2];
      push(userMsg('✅ Отметить выполненным'));
      advanceWorkflow(wfId, 'passed');
    }

    else if (action.startsWith('step_fail_')) {
      const parts = action.split('_');
      const wfId  = parts[2];
      push(userMsg('❌ Шаг провален'));
      advanceWorkflow(wfId, 'failed');
    }

    else if (action.startsWith('step_timeout_')) {
      const parts = action.split('_');
      const wfId  = parts[2];
      typeAndPush(300,
        botMsg(
          '⏰ Время истекло! Шаг не завершён в срок.',
          [[
            { label: '✅ Всё равно выполнен', action: `step_done_${wfId}_0`, variant: 'success' },
            { label: '❌ Провален',            action: `step_fail_${wfId}_0`, variant: 'danger' },
          ]]
        )
      );
    }

    else if (action.startsWith('approve_')) {
      const [, wfId, idxStr] = action.split('_');
      const idx = Number(idxStr);
      push(userMsg('▶ Продолжить'));
      setWorkflows(prev => {
        const wf = prev.find(w => w.id === wfId);
        if (!wf) return prev;
        const nextStep = wf.steps[idx];
        if (!nextStep) return prev;
        typeAndPush(600,
          botMsg(
            `▶ Шаг ${idx + 1}: ${nextStep.name}\n⏱ ${nextStep.timeLimitMinutes} мин\n🤖 ${nextStep.modelId}`,
            [[
              { label: '✅ Выполнен', action: `step_done_${wfId}_${idx}`, variant: 'success' },
              { label: '❌ Провален', action: `step_fail_${wfId}_${idx}`, variant: 'danger' },
            ]]
          )
        );
        setTimeout(() => startStepTimer(nextStep.id, nextStep.timeLimitMinutes, () => {
          handleAction(`step_timeout_${wfId}_${idx}`);
        }), 700);
        return prev.map(w => w.id === wfId
          ? { ...w, steps: w.steps.map((s, i) => i === idx ? { ...s, status: 'running' } : s) }
          : w
        );
      });
    }

    else if (action.startsWith('pause_')) {
      const wfId = action.replace('pause_', '');
      push(userMsg('⏸ Пауза'));
      updateWorkflow(wfId, w => ({ ...w, status: 'paused' }));
      typeAndPush(400,
        botMsg('⏸ Алгоритм на паузе.',
          [[
            { label: '▶ Продолжить', action: `resume_${wfId}`, variant: 'primary' },
            { label: '⬅️ Меню',      action: 'cmd_menu' },
          ]]
        )
      );
    }

    else if (action.startsWith('resume_')) {
      const wfId = action.replace('resume_', '');
      push(userMsg('▶ Продолжить'));
      setWorkflows(prev => {
        const wf = prev.find(w => w.id === wfId);
        if (!wf) return prev;
        const step = wf.steps[wf.currentStepIndex];
        if (step) {
          typeAndPush(500,
            botMsg(`▶ Продолжаем: Шаг ${wf.currentStepIndex + 1}: ${step.name}`,
              [[
                { label: '✅ Выполнен', action: `step_done_${wfId}_${wf.currentStepIndex}`, variant: 'success' },
                { label: '❌ Провален', action: `step_fail_${wfId}_${wf.currentStepIndex}`, variant: 'danger' },
              ]]
            )
          );
        }
        return prev.map(w => w.id === wfId ? { ...w, status: 'running' } : w);
      });
    }

    else if (action.startsWith('report_')) {
      const wfId = action.replace('report_', '');
      const wf   = workflowsRef.current.find(w => w.id === wfId);
      if (!wf) return;
      const passed = wf.steps.filter(s => s.result === 'passed').length;
      const failed = wf.steps.filter(s => s.result === 'failed').length;
      push(userMsg('📊 Итоговый отчёт'));
      typeAndPush(600,
        botMsg(
          `📊 Отчёт: «${wf.name}»\n\n` +
          wf.steps.map((s, i) => `${s.result === 'passed' ? '✅' : s.result === 'failed' ? '❌' : '⬜'} Шаг ${i + 1}: ${s.name}`).join('\n') +
          `\n\nИтого: ✅${passed} ❌${failed} из ${wf.steps.length} шагов`,
          [[{ label: '⬅️ Меню', action: 'cmd_menu' }]]
        )
      );
    }

    else if (action.startsWith('restart_')) {
      handleRunWorkflow(action.replace('restart_', ''), true);
    }

    else if (action === 'add_server_hint' || action === 'create_workflow_hint') {
      push(userMsg('Открыть панель'));
      typeAndPush(400, botMsg('Используй панель справа →', [[{ label: '⬅️ Меню', action: 'cmd_menu' }]]));
    }

    // ── File zone actions ──────────────────────────────────────────────────
    else if (action === 'cmd_file_download') {
      push(userMsg('⬇️ Скачать файл'));
      triggerFileDownload();
    }

    else if (action === 'cmd_file_delete') {
      push(userMsg('🗑 Удалить файл'));
      setHasZipFile(false);
      typeAndPush(300, botMsg('🗑 Файл удалён из зоны передачи.', [[{ label: '⬅️ Меню', action: 'cmd_menu' }]]));
    }
  };

  // sync impl ref every render
  useEffect(() => { implRef.current = handleActionImpl; });

  // ── Workflow management ────────────────────────────────────────────────────

  const createWorkflow = useCallback((data: any) => {
    const wf: Workflow = {
      id: `wf_${Date.now()}`,
      ...data,
      status: 'idle',
      currentStepIndex: 0,
      startedAt: null,
      createdAt: new Date().toISOString(),
    };
    setWorkflows(prev => [...prev, wf]);
    typeAndPush(500,
      botMsg(
        `✅ Алгоритм «${wf.name}» создан!\n${wf.steps.length} шагов | Авто: ${wf.autoContinue ? 'ВКЛ' : 'ВЫКЛ'}`,
        [[
          { label: `▶ Запустить`, action: `run_workflow_${wf.id}`, variant: 'primary' },
          { label: '⬅️ Меню',     action: 'cmd_menu' },
        ]]
      )
    );
  }, [typeAndPush]);

  const deleteWorkflow     = useCallback((id: string) => setWorkflows(prev => prev.filter(w => w.id !== id)), []);
  const toggleAutoContinue = useCallback((id: string) => setWorkflows(prev => prev.map(w => w.id === id ? { ...w, autoContinue: !w.autoContinue } : w)), []);

  useEffect(() => {
    return () => { Object.values(timerRefs.current).forEach(clearInterval); };
  }, []);

  // ── FileZone callbacks ────────────────────────────────────────────────────

  const handleFileRead = useCallback((filename: string, entries: ZipEntry[], _blob: File) => {
    setHasZipFile(true);
    const dirs  = entries.filter(e => e.isDir).length;
    const files = entries.filter(e => !e.isDir).length;
    const total = entries.filter(e => !e.isDir).reduce((s, e) => s + e.size, 0);
    const fmtSize = (b: number) =>
      b >= 1024 * 1024 ? `${(b / 1024 / 1024).toFixed(1)} MB`
      : b >= 1024 ? `${(b / 1024).toFixed(1)} KB` : `${b} B`;

    // Top-level items
    const topLevel = entries
      .filter(e => {
        const parts = e.path.split('/').filter(Boolean);
        return parts.length === (e.isDir ? 1 : 1);
      })
      .slice(0, 12)
      .map(e => {
        const name = e.path.split('/').filter(Boolean)[0];
        return `${e.isDir ? '📁' : '📄'} ${name}`;
      });

    push(userMsg(`📎 ${filename}`));
    typeAndPush(400, botMsg(
      `📦 ${filename}\n\n`
      + `📁 Папок: ${dirs} | 📄 Файлов: ${files} | 💾 ${fmtSize(total)}\n\n`
      + `Содержимое (верхний уровень):\n${topLevel.join('\n')}`
      + (entries.length > 12 ? `\n...и ещё ${entries.length - 12} элементов` : ''),
      [[
        { label: '⬇️ Скачать файл',  action: 'cmd_file_download', variant: 'primary' },
        { label: '🗑 Удалить файл',   action: 'cmd_file_delete',   variant: 'danger' },
        { label: '⬅️ Меню',           action: 'cmd_menu' },
      ]]
    ));
  }, [push, typeAndPush]);

  // ── input bar props ────────────────────────────────────────────────────────

  const slot = inputMode ? aiSlots.find(s => s.id === inputMode.slotId) : undefined;
  const inputPlaceholder = inputMode?.type === 'zadanie'
    ? `Задание для слота ${inputMode.slotId}${slot?.name ? ` (${slot.name})` : ''}...`
    : inputMode?.type === 'message'
    ? `Сообщение для ${slot?.name || 'ИИ'} (заберёт через probe URL)...`
    : inputMode?.type === 'direct_chat'
    ? `Прямой чат с ${slot?.name || 'ИИ'} (нужен API-ключ)...`
    : inputMode?.type === 'apikey'
    ? `sk-ant-... или другой API-ключ`
    : undefined;

  return (
    <div className="flex h-full">
      <div className="flex-1 min-w-0" style={{ maxWidth: '60%' }}>
        <TelegramChat
          messages={messages}
          onAction={handleAction}
          onSend={inputMode ? handleSend : undefined}
          onFileAttach={handleFileRead}
          onCompareResult={(result) => {
            push(botMsg(result, [[
              { label: '🔀 Сравнить снова', action: 'noop' },
              { label: '⬅️ Меню', action: 'cmd_menu' },
            ]]));
          }}
          serverUrl={serverUrl}
          inputPlaceholder={inputPlaceholder}
          inputActive={!!inputMode}
        />
      </div>
      <div className="w-[40%] shrink-0 overflow-hidden">
        <WorkflowPanel
          workflows={workflows}
          activeWorkflow={activeWorkflow}
          onCreateWorkflow={createWorkflow}
          onRunWorkflow={(id) => handleRunWorkflow(id)}
          onSelectWorkflow={setActiveWorkflowId}
          onDeleteWorkflow={deleteWorkflow}
          onToggleAutoContinue={toggleAutoContinue}
          stepTimers={stepTimers}
        />
      </div>
    </div>
  );
}
