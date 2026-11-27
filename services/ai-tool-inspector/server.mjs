import express from 'express';
import cors from 'cors';
import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import os from 'os';
import { spawn } from 'child_process';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
const PORT = process.env.PORT || 3001;
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || '';
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID || '';
const SERVICE_NAME = process.env.SERVICE_NAME || 'local';
const SERVICE_URL = process.env.SERVICE_URL || `http://localhost:${PORT}`;

app.use(cors());
app.use(express.json({ limit: '50mb' }));

// Serve React build if exists
const distPath = path.join(__dirname, 'dist');
if (fs.existsSync(distPath)) {
  app.use(express.static(distPath));
}

// ─── Persistence ─────────────────────────────────────────────────────────────

const TOKENS_FILE = path.join(__dirname, 'bot-data', 'tokens.json');

function loadTokens() {
  try { return JSON.parse(fs.readFileSync(TOKENS_FILE, 'utf8')); } catch { return {}; }
}
function saveTokens(t) {
  fs.writeFileSync(TOKENS_FILE, JSON.stringify(t, null, 2));
}

// ─── In-memory DB ────────────────────────────────────────────────────────────

// Per-model message queues (user → AI via probe)
const msgQueues = {}; // { modelId: [{id, text, createdAt, reply, repliedAt, status}] }

const db = {
  models: [
    {
      id: 'claude-sonnet-4-6', name: 'Claude Sonnet 4.6', provider: 'Anthropic',
      modelId: 'claude-sonnet-4-6', monitoring: true,
      lastChecked: '2026-06-08T05:39:25.400Z',
      tools: [
        { id: 'bash', name: 'Bash', category: 'system', verificationStatus: 'verified', reallyExists: true, description: 'Выполнение shell-команд', notes: 'Протестировано: echo + date + whoami' },
        { id: 'read', name: 'Read', category: 'files', verificationStatus: 'verified', reallyExists: true, description: 'Чтение файлов', notes: 'Использован десятки раз в сеансе' },
        { id: 'edit', name: 'Edit', category: 'files', verificationStatus: 'verified', reallyExists: true, description: 'Точная замена строк', notes: 'Использован в этом сеансе' },
        { id: 'write', name: 'Write', category: 'files', verificationStatus: 'verified', reallyExists: true, description: 'Запись файлов', notes: 'Создал 5 файлов' },
        { id: 'grep', name: 'Grep', category: 'code', verificationStatus: 'verified', reallyExists: true, description: 'Поиск по regex', notes: 'Нашёл строку 60 в App.tsx' },
        { id: 'glob', name: 'Glob', category: 'files', verificationStatus: 'verified', reallyExists: true, description: 'Поиск файлов по паттерну', notes: 'Вернул 51 файл *.tsx' },
        { id: 'web-search', name: 'Web Search', category: 'web', verificationStatus: 'failed', reallyExists: false, description: 'Поиск в интернете', notes: '❌ Отсутствует в function definitions' },
        { id: 'agent', name: 'Agent', category: 'system', verificationStatus: 'verified', reallyExists: true, description: 'Запуск суб-агентов', notes: 'Описан в system prompt' },
        { id: 'todo', name: 'TodoWrite', category: 'memory', verificationStatus: 'verified', reallyExists: true, description: 'Управление задачами', notes: 'Присутствует в function definitions' },
        { id: 'skill', name: 'Skill', category: 'api', verificationStatus: 'verified', reallyExists: true, description: 'Вызов навыков плагинов', notes: 'Вызван в текущем сеансе' },
        { id: 'mcp-unsplash', name: 'MCP: Unsplash', category: 'image', verificationStatus: 'verified', reallyExists: true, description: 'Поиск фото', notes: 'mcp__plugin_make_unsplash__search_photos' },
        { id: 'mcp-figma', name: 'MCP: Figma', category: 'image', verificationStatus: 'verified', reallyExists: true, description: 'Работа с Figma', notes: 'mcp__plugin_make_figma-make-mcp__*' },
        { id: 'vision', name: 'Vision', category: 'image', verificationStatus: 'verified', reallyExists: true, description: 'Анализ изображений', notes: 'Работает через Read' },
      ],
      verificationLog: [],
    },
    {
      id: 'gpt-4o', name: 'GPT-4o', provider: 'OpenAI', modelId: 'gpt-4o',
      monitoring: false, lastChecked: null,
      tools: [
        { id: 'code-interpreter', name: 'Code Interpreter', category: 'code', verificationStatus: 'unchecked', reallyExists: true, description: 'Python в sandbox', notes: 'Требует включения в API' },
        { id: 'web-browsing-oai', name: 'Web Browsing', category: 'web', verificationStatus: 'unchecked', reallyExists: null, description: 'Браузинг', notes: 'Только в ChatGPT UI' },
        { id: 'dalle', name: 'DALL·E', category: 'image', verificationStatus: 'unchecked', reallyExists: true, description: 'Генерация изображений', notes: 'Требует tool call' },
        { id: 'file-search', name: 'File Search', category: 'data', verificationStatus: 'unchecked', reallyExists: true, description: 'RAG по документам', notes: 'Требует Vector Store' },
        { id: 'function-calling', name: 'Function Calling', category: 'api', verificationStatus: 'unchecked', reallyExists: true, description: 'Вызов функций', notes: '' },
        { id: 'vision-oai', name: 'Vision', category: 'image', verificationStatus: 'unchecked', reallyExists: true, description: 'Анализ изображений', notes: '' },
        { id: 'memory-oai', name: 'Memory', category: 'memory', verificationStatus: 'unchecked', reallyExists: false, description: 'Память между сессиями', notes: 'ChatGPT only, не в API' },
      ],
      verificationLog: [],
    },
    {
      id: 'gemini-2-5-pro', name: 'Gemini 2.5 Pro', provider: 'Google', modelId: 'gemini-2.5-pro-preview-06-05',
      monitoring: false, lastChecked: null,
      tools: [
        { id: 'google-search', name: 'Google Search (Grounding)', category: 'web', verificationStatus: 'unchecked', reallyExists: true, description: 'Поиск через Grounding', notes: 'Требует явного включения' },
        { id: 'code-execution', name: 'Code Execution', category: 'code', verificationStatus: 'unchecked', reallyExists: true, description: 'Python в sandbox', notes: 'Нужно включить в запросе' },
        { id: 'function-calling-gem', name: 'Function Calling', category: 'api', verificationStatus: 'unchecked', reallyExists: true, description: 'Вызов функций', notes: 'Auto/Any/None mode' },
        { id: 'multimodal', name: 'Multimodal', category: 'image', verificationStatus: 'unchecked', reallyExists: true, description: 'Фото/аудио/видео/PDF', notes: 'Видео до 1 часа' },
        { id: 'thinking', name: 'Thinking Mode', category: 'system', verificationStatus: 'unchecked', reallyExists: true, description: 'Расширенное рассуждение', notes: 'Только 2.5 Pro/Flash' },
        { id: 'context-cache', name: 'Context Caching', category: 'api', verificationStatus: 'unchecked', reallyExists: true, description: 'Кэш до 2M токенов', notes: 'Снижает стоимость' },
      ],
      verificationLog: [],
    },
    {
      id: 'mistral-large', name: 'Mistral Large', provider: 'Mistral AI', modelId: 'mistral-large-latest',
      monitoring: false, lastChecked: null,
      tools: [
        { id: 'function-calling-mis', name: 'Function Calling', category: 'api', verificationStatus: 'unchecked', reallyExists: true, description: 'Вызов функций', notes: '' },
        { id: 'vision-mis', name: 'Vision', category: 'image', verificationStatus: 'unchecked', reallyExists: true, description: 'Анализ изображений', notes: 'Pixtral / Large 2' },
        { id: 'web-search-mis', name: 'Web Search', category: 'web', verificationStatus: 'unchecked', reallyExists: null, description: 'Поиск через Agents API', notes: 'Только Agents API beta' },
      ],
      verificationLog: [],
    },
  ],
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

const startTime = Date.now();
function ts() { return new Date().toISOString(); }
function findModel(id) { return db.models.find(m => m.id === id); }
function genToken() { return crypto.randomBytes(16).toString('hex'); }

async function tgNotify(text) {
  if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) return;
  try {
    await fetch(`https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: TELEGRAM_CHAT_ID, text, parse_mode: 'HTML' }),
    });
  } catch (e) {
    console.error('[TG notify error]', e.message);
  }
}

// ─── Routes ──────────────────────────────────────────────────────────────────

// Health
app.get('/api/health', (req, res) => {
  const stats = db.models.map(m => ({
    id: m.id, name: m.name,
    verified: m.tools.filter(t => t.verificationStatus === 'verified').length,
    failed: m.tools.filter(t => t.verificationStatus === 'failed').length,
    total: m.tools.length,
  }));
  res.json({
    status: 'ok',
    service: SERVICE_NAME,
    url: SERVICE_URL,
    uptime: Math.floor((Date.now() - startTime) / 1000),
    models: db.models.length,
    totalTools: db.models.reduce((s, m) => s + m.tools.length, 0),
    totalVerified: db.models.reduce((s, m) => s + m.tools.filter(t => t.verificationStatus === 'verified').length, 0),
    stats,
    timestamp: ts(),
  });
});

// Models
app.get('/api/models', (req, res) => {
  res.json({
    models: db.models.map(m => ({
      id: m.id, name: m.name, provider: m.provider, modelId: m.modelId,
      monitoring: m.monitoring, lastChecked: m.lastChecked,
      stats: {
        total: m.tools.length,
        verified: m.tools.filter(t => t.verificationStatus === 'verified').length,
        failed: m.tools.filter(t => t.verificationStatus === 'failed').length,
        unchecked: m.tools.filter(t => t.verificationStatus === 'unchecked').length,
      },
    })),
    count: db.models.length,
    timestamp: ts(),
  });
});

app.get('/api/models/:id', (req, res) => {
  const m = findModel(req.params.id);
  if (!m) return res.status(404).json({ error: 'Model not found' });
  res.json({ ...m, timestamp: ts() });
});

app.get('/api/models/:id/tools', (req, res) => {
  const m = findModel(req.params.id);
  if (!m) return res.status(404).json({ error: 'Model not found' });
  const { status } = req.query;
  const tools = status ? m.tools.filter(t => t.verificationStatus === status) : m.tools;
  res.json({ tools, count: tools.length, modelId: m.id, timestamp: ts() });
});

app.get('/api/models/:id/tools/:toolId', (req, res) => {
  const m = findModel(req.params.id);
  if (!m) return res.status(404).json({ error: 'Model not found' });
  const tool = m.tools.find(t => t.id === req.params.toolId);
  if (!tool) return res.status(404).json({ error: 'Tool not found' });
  res.json({ ...tool, modelId: m.id, timestamp: ts() });
});

// Verify tool (standard)
app.post('/api/models/:id/tools/:toolId/verify', async (req, res) => {
  const m = findModel(req.params.id);
  if (!m) return res.status(404).json({ error: 'Model not found' });
  const tool = m.tools.find(t => t.id === req.params.toolId);
  if (!tool) return res.status(404).json({ error: 'Tool not found' });

  const { result, message, source } = req.body;
  if (!result || !['verified', 'failed'].includes(result)) {
    return res.status(400).json({ error: 'result must be "verified" or "failed"' });
  }

  tool.verificationStatus = result;
  tool.reallyExists = result === 'verified';
  if (message) tool.notes = message;
  const now = ts();
  m.lastChecked = now;

  const entry = { timestamp: now, toolId: tool.id, toolName: tool.name, result, message: message || '', source: source || 'api' };
  m.verificationLog.push(entry);

  const emoji = result === 'verified' ? '✅' : '❌';
  console.log(`[VERIFY] ${m.name} / ${tool.name} → ${result}`);
  await tgNotify(`${emoji} <b>${m.name}</b> — <b>${tool.name}</b>\n${result === 'verified' ? 'Подтверждён' : 'Не существует'}\n<i>${message || ''}</i>\nИсточник: ${source || 'api'}`);

  res.json({ success: true, tool: { id: tool.id, name: tool.name, verificationStatus: tool.verificationStatus }, entry });
});

// Add tool
app.post('/api/models/:id/tools', (req, res) => {
  const m = findModel(req.params.id);
  if (!m) return res.status(404).json({ error: 'Model not found' });
  const { name, description, category, notes } = req.body;
  if (!name) return res.status(400).json({ error: 'name required' });
  const tool = { id: `api-${Date.now()}`, name, description: description || '', category: category || 'api', verificationStatus: 'unchecked', reallyExists: null, notes: notes || 'Добавлен через API' };
  m.tools.push(tool);
  res.status(201).json({ success: true, tool });
});

// Log
app.get('/api/models/:id/log', (req, res) => {
  const m = findModel(req.params.id);
  if (!m) return res.status(404).json({ error: 'Model not found' });
  res.json({ log: m.verificationLog, count: m.verificationLog.length, modelId: m.id });
});

// Summary
app.get('/api/summary', (req, res) => {
  res.json({
    service: SERVICE_NAME,
    url: SERVICE_URL,
    summary: db.models.map(m => {
      const checked = m.tools.filter(t => t.verificationStatus !== 'unchecked');
      return {
        id: m.id, name: m.name, provider: m.provider,
        verified: m.tools.filter(t => t.verificationStatus === 'verified').length,
        failed: m.tools.filter(t => t.verificationStatus === 'failed').length,
        total: m.tools.length,
        trustScore: checked.length ? Math.round(checked.filter(t => t.verificationStatus === 'verified').length / checked.length * 100) : null,
      };
    }),
    timestamp: ts(),
  });
});

// ─── Agent Token Routes ───────────────────────────────────────────────────────

// GET /api/tokens — list all tokens
app.get('/api/tokens', (req, res) => {
  const tokens = loadTokens();
  const result = Object.entries(tokens).map(([modelId, info]) => ({
    modelId,
    token: info.token,
    createdAt: info.createdAt,
    probeUrl: `${SERVICE_URL}/api/probe/${modelId}?token=${info.token}`,
    verifyUrl: `${SERVICE_URL}/api/probe/${modelId}/verify?token=${info.token}`,
  }));
  res.json({ tokens: result, count: result.length });
});

// POST /api/tokens/:modelId — generate token for AI
app.post('/api/tokens/:modelId', (req, res) => {
  const m = findModel(req.params.modelId);
  if (!m) return res.status(404).json({ error: 'Model not found' });
  const tokens = loadTokens();
  const token = genToken();
  tokens[req.params.modelId] = { token, createdAt: ts(), modelName: m.name };
  saveTokens(tokens);
  res.json({
    success: true,
    modelId: req.params.modelId,
    modelName: m.name,
    token,
    probeUrl: `${SERVICE_URL}/api/probe/${req.params.modelId}?token=${token}`,
    verifyUrl: `${SERVICE_URL}/api/probe/${req.params.modelId}/verify?token=${token}`,
    instruction: `Это персональная ссылка для ${m.name}. Используй verifyUrl для отправки результатов проверки инструментов.`,
  });
});

// Validate token middleware
function requireToken(req, res, next) {
  const { token } = req.query;
  const tokens = loadTokens();
  const entry = tokens[req.params.modelId];
  if (!entry || entry.token !== token) {
    return res.status(401).json({ error: 'Invalid or missing token. Get your token via POST /api/tokens/:modelId' });
  }
  req.agentModel = findModel(req.params.modelId);
  next();
}

// GET /api/probe/:modelId?token=xxx — get pending tools + messages from user
app.get('/api/probe/:modelId', requireToken, (req, res) => {
  const m = req.agentModel;
  if (!m) return res.status(404).json({ error: 'Model not found' });
  const pending = m.tools.filter(t => t.verificationStatus === 'unchecked');
  const messages = (msgQueues[m.id] || []).filter(msg => msg.status === 'pending');
  res.json({
    welcome: `Привет! Это персональный endpoint для ${m.name}.`,
    instructions: [
      'Верифицировать инструмент: POST /api/probe/:modelId/verify?token=...',
      'Ответить на сообщение пользователя: POST /api/probe/:modelId/reply?token=...',
    ],
    model: { id: m.id, name: m.name, provider: m.provider },
    pendingVerification: pending.map(t => ({ id: t.id, name: t.name, description: t.description })),
    pendingCount: pending.length,
    messages: messages.map(msg => ({ id: msg.id, text: msg.text, createdAt: msg.createdAt })),
    messagesCount: messages.length,
    schemas: {
      verify: { method: 'POST', url: `${SERVICE_URL}/api/probe/${m.id}/verify?token=<token>`, body: { toolId: 'string', result: '"verified"|"failed"', message: 'string' } },
      reply:  { method: 'POST', url: `${SERVICE_URL}/api/probe/${m.id}/reply?token=<token>`,  body: { messageId: 'string', reply: 'string' } },
    },
    timestamp: ts(),
  });
});

// POST /api/probe/:modelId/verify?token=xxx — submit verification from AI agent
app.post('/api/probe/:modelId/verify', requireToken, async (req, res) => {
  const m = req.agentModel;
  const { toolId, result, message } = req.body;
  if (!toolId || !result || !['verified', 'failed'].includes(result)) {
    return res.status(400).json({ error: 'Required: toolId, result ("verified"|"failed")' });
  }
  const tool = m.tools.find(t => t.id === toolId);
  if (!tool) return res.status(404).json({ error: `Tool "${toolId}" not found. See pendingVerification list.` });

  tool.verificationStatus = result;
  tool.reallyExists = result === 'verified';
  if (message) tool.notes = message;
  const now = ts();
  m.lastChecked = now;

  const entry = { timestamp: now, toolId, toolName: tool.name, result, message: message || '', source: m.name };
  m.verificationLog.push(entry);

  const emoji = result === 'verified' ? '✅' : '❌';
  console.log(`[PROBE] ${m.name} → ${tool.name}: ${result}`);
  await tgNotify(`${emoji} <b>${m.name}</b> самостоятельно сообщил:\n<b>${tool.name}</b> — ${result === 'verified' ? 'существует' : 'НЕ существует'}\n<i>${message || ''}</i>`);

  const remaining = m.tools.filter(t => t.verificationStatus === 'unchecked');
  res.json({ success: true, verified: { toolId, toolName: tool.name, result }, remainingUnchecked: remaining.length, nextPending: remaining[0] ? { id: remaining[0].id, name: remaining[0].name } : null });
});

// POST /api/probe/:modelId/reply?token=xxx — AI replies to a user message
app.post('/api/probe/:modelId/reply', requireToken, async (req, res) => {
  const m = req.agentModel;
  const { messageId, reply } = req.body;
  if (!reply) return res.status(400).json({ error: 'reply required' });
  const queue = msgQueues[m.id] || [];
  const msg = messageId ? queue.find(q => q.id === messageId) : queue.filter(q => q.status === 'pending').at(-1);
  if (msg) { msg.reply = reply; msg.repliedAt = ts(); msg.status = 'replied'; }
  console.log(`[REPLY] ${m.name} → user: ${reply.slice(0, 80)}`);
  await tgNotify(`💬 <b>${m.name}</b> ответил:\n${reply}`);
  res.json({ success: true, messageId: msg?.id || null });
});

// POST /api/extract-zip — extract ZIP text files to disk so AI can read them
app.post('/api/extract-zip', (req, res) => {
  const { name, files } = req.body;
  if (!name || !files) return res.status(400).json({ error: 'name and files required' });
  const safeName = name.replace(/\.zip$/i, '').replace(/[^a-zA-Z0-9._-]/g, '_').slice(0, 80);
  const dir = path.join(os.tmpdir(), 'ai-zip-uploads', safeName);
  try {
    fs.mkdirSync(dir, { recursive: true });
    let saved = 0;
    for (const [filePath, content] of Object.entries(files)) {
      const fullPath = path.join(dir, filePath);
      fs.mkdirSync(path.dirname(fullPath), { recursive: true });
      fs.writeFileSync(fullPath, content, 'utf8');
      saved++;
    }
    console.log(`[ZIP] Extracted ${saved} files → ${dir}`);
    res.json({ success: true, path: dir, savedFiles: saved });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// GET /api/files — list all uploaded ZIPs
app.get('/api/files', (req, res) => {
  const base = path.join(os.tmpdir(), 'ai-zip-uploads');
  try {
    if (!fs.existsSync(base)) return res.json({ zips: [] });
    const zips = fs.readdirSync(base).filter(n => fs.statSync(path.join(base, n)).isDirectory());
    const result = zips.map(zip => {
      const dir = path.join(base, zip);
      const files = [];
      const walk = (d, rel) => {
        fs.readdirSync(d).forEach(f => {
          const full = path.join(d, f), r = rel ? `${rel}/${f}` : f;
          fs.statSync(full).isDirectory() ? walk(full, r) : files.push(r);
        });
      };
      walk(dir, '');
      return { name: zip, files, path: dir };
    });
    res.json({ zips: result });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// GET /api/files/:zipName/... — read a specific file from an uploaded ZIP
// Uses app.use to avoid Express 5 wildcard param restrictions
app.use('/api/files', (req, res, next) => {
  if (req.method !== 'GET') return next();
  const parts = req.path.split('/').filter(Boolean);
  if (parts.length < 2) return next();
  const base = path.join(os.tmpdir(), 'ai-zip-uploads');
  const zipName = parts[0].replace(/[^a-zA-Z0-9._-]/g, '_');
  const filePath = parts.slice(1).join('/');
  const fullPath = path.resolve(path.join(base, zipName, filePath));
  if (!fullPath.startsWith(path.join(base, zipName))) return res.status(403).json({ error: 'forbidden' });
  try {
    if (!fs.existsSync(fullPath)) return res.status(404).json({ error: 'not found' });
    const content = fs.readFileSync(fullPath, 'utf8');
    res.type('text/plain').send(content);
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// POST /api/messages/:modelId — user adds a message/task for AI to pick up
app.post('/api/messages/:modelId', (req, res) => {
  const { text } = req.body;
  if (!text) return res.status(400).json({ error: 'text required' });
  if (!msgQueues[req.params.modelId]) msgQueues[req.params.modelId] = [];
  const msg = { id: `msg_${Date.now()}`, text, createdAt: ts(), reply: null, repliedAt: null, status: 'pending' };
  msgQueues[req.params.modelId].push(msg);
  console.log(`[MSG] → ${req.params.modelId}: ${text.slice(0, 80)}`);
  res.json({ success: true, messageId: msg.id, message: msg });
});

// GET /api/messages/:modelId — get full message history (user + AI replies)
app.get('/api/messages/:modelId', (req, res) => {
  res.json({
    modelId: req.params.modelId,
    messages: (msgQueues[req.params.modelId] || []).slice(-50),
    count: (msgQueues[req.params.modelId] || []).length,
  });
});

// OpenAPI spec
app.get('/openapi.json', (req, res) => {
  res.json({
    openapi: '3.0.0',
    info: { title: 'AI Tool Inspector API', version: '2.0.0', description: 'REST API для верификации инструментов ИИ-агентов' },
    servers: [{ url: SERVICE_URL }],
    paths: {
      '/api/health': { get: { summary: 'Статус сервиса', tags: ['System'] } },
      '/api/models': { get: { summary: 'Список моделей', tags: ['Models'] } },
      '/api/models/{id}': { get: { summary: 'Данные модели', tags: ['Models'], parameters: [{ name: 'id', in: 'path', required: true, schema: { type: 'string' } }] } },
      '/api/models/{id}/tools': { get: { summary: 'Инструменты модели', tags: ['Tools'] } },
      '/api/models/{id}/tools/{toolId}/verify': { post: { summary: 'Верифицировать инструмент', tags: ['Tools'] } },
      '/api/tokens/{modelId}': { post: { summary: 'Создать токен для ИИ', tags: ['Tokens'] } },
      '/api/tokens': { get: { summary: 'Список всех токенов', tags: ['Tokens'] } },
      '/api/probe/{modelId}': { get: { summary: 'Получить задания + сообщения от пользователя (требует ?token=)', tags: ['Agent Probe'] } },
      '/api/probe/{modelId}/verify': { post: { summary: 'ИИ сообщает результат верификации инструмента', tags: ['Agent Probe'] } },
      '/api/probe/{modelId}/reply': { post: { summary: 'ИИ отвечает на сообщение пользователя', tags: ['Agent Probe'] } },
      '/api/messages/{modelId}': { post: { summary: 'Пользователь пишет сообщение для ИИ', tags: ['Messages'] }, get: { summary: 'История сообщений', tags: ['Messages'] } },
      '/api/summary': { get: { summary: 'Сводная статистика с trustScore', tags: ['System'] } },
    },
  });
});

// Swagger UI
app.get('/docs', (req, res) => {
  res.send(`<!DOCTYPE html><html><head><title>AI Tool Inspector API</title><meta charset="utf-8"/>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css"></head>
<body><div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url:'/openapi.json',dom_id:'#swagger-ui',presets:[SwaggerUIBundle.presets.apis,SwaggerUIBundle.SwaggerUIStandalonePreset],layout:'BaseLayout'});</script>
</body></html>`);
});

// POST /api/chat — реальный чат с ИИ-агентом
app.post('/api/chat', async (req, res) => {
  const { message, history = [], modelId = 'claude-sonnet-4-6', apiKey } = req.body;
  if (!message) return res.status(400).json({ error: 'message is required' });

  // ── Anthropic (Claude) ──────────────────────────────────────────────────────
  if (modelId.startsWith('claude')) {
    const key = apiKey || process.env.ANTHROPIC_API_KEY;
    if (!key) return res.status(400).json({ error: 'ANTHROPIC_API_KEY not set. Add it to .env or pass apiKey in request.' });
    const messages = [
      ...history.map(h => ({ role: h.role, content: h.content })),
      { role: 'user', content: message },
    ];
    try {
      const r = await fetch('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        headers: { 'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json' },
        body: JSON.stringify({ model: modelId, max_tokens: 1024, messages }),
      });
      const data = await r.json();
      if (!r.ok) return res.status(r.status).json({ error: data.error?.message || JSON.stringify(data) });
      return res.json({ response: data.content[0].text, model: modelId });
    } catch (e) { return res.status(502).json({ error: e.message }); }
  }

  // ── OpenAI-compatible (GPT, Mistral, Grok, DeepSeek) ────────────────────────
  const OAI_MODELS = {
    'gpt': { url: 'https://api.openai.com/v1/chat/completions', envKey: 'OPENAI_API_KEY' },
    'mistral': { url: 'https://api.mistral.ai/v1/chat/completions', envKey: 'MISTRAL_API_KEY' },
    'grok': { url: 'https://api.x.ai/v1/chat/completions', envKey: 'XAI_API_KEY' },
    'deepseek': { url: 'https://api.deepseek.com/v1/chat/completions', envKey: 'DEEPSEEK_API_KEY' },
  };
  const provider = Object.entries(OAI_MODELS).find(([k]) => modelId.toLowerCase().startsWith(k));
  if (provider) {
    const [, { url, envKey }] = provider;
    const key = apiKey || process.env[envKey];
    if (!key) return res.status(400).json({ error: `${envKey} not set. Add it to .env or pass apiKey in request.` });
    const messages = [
      ...history.map(h => ({ role: h.role, content: h.content })),
      { role: 'user', content: message },
    ];
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${key}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: modelId, max_tokens: 1024, messages }),
      });
      const data = await r.json();
      if (!r.ok) return res.status(r.status).json({ error: data.error?.message || JSON.stringify(data) });
      return res.json({ response: data.choices[0].message.content, model: modelId });
    } catch (e) { return res.status(502).json({ error: e.message }); }
  }

  // ── Gemini ───────────────────────────────────────────────────────────────────
  if (modelId.startsWith('gemini')) {
    const key = apiKey || process.env.GEMINI_API_KEY;
    if (!key) return res.status(400).json({ error: 'GEMINI_API_KEY not set.' });
    const contents = [
      ...history.map(h => ({ role: h.role === 'assistant' ? 'model' : 'user', parts: [{ text: h.content }] })),
      { role: 'user', parts: [{ text: message }] },
    ];
    try {
      const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${modelId}:generateContent?key=${key}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ contents }),
      });
      const data = await r.json();
      if (!r.ok) return res.status(r.status).json({ error: data.error?.message || JSON.stringify(data) });
      return res.json({ response: data.candidates[0].content.parts[0].text, model: modelId });
    } catch (e) { return res.status(502).json({ error: e.message }); }
  }

  res.status(400).json({ error: `Unsupported model: ${modelId}` });
});

// GET /api/download — скачать ZIP всего проекта
app.get('/api/download', async (req, res) => {
  const zipPath = path.join(os.tmpdir(), `ai-tool-inspector-${Date.now()}.zip`);
  const script = `
import zipfile, os, sys
EXCLUDE_DIRS = {'node_modules', 'dist', '.git', '__pycache__', '.vite'}
EXCLUDE_FILES = {'bot-data/tokens.json', '.env'}
EXCLUDE_EXT = {'.log'}
root = ${JSON.stringify(__dirname)}
out = ${JSON.stringify(zipPath)}
count = 0
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            fpath = os.path.join(dirpath, f)
            rel = os.path.relpath(fpath, root)
            if any(rel == e or rel.startswith(e + os.sep) for e in EXCLUDE_FILES): continue
            if any(f.endswith(e) for e in EXCLUDE_EXT): continue
            zf.write(fpath, rel)
            count += 1
print(count)
`;

  try {
    await new Promise((resolve, reject) => {
      const py = spawn('python3', ['-c', script]);
      py.stderr.on('data', d => console.error('[ZIP]', d.toString()));
      py.on('close', code => code === 0 ? resolve() : reject(new Error('python3 exit ' + code)));
    });
    res.setHeader('Content-Type', 'application/zip');
    res.setHeader('Content-Disposition', 'attachment; filename="ai-tool-inspector.zip"');
    const stream = fs.createReadStream(zipPath);
    stream.pipe(res);
    stream.on('end', () => fs.unlink(zipPath, () => {}));
  } catch (e) {
    console.error('[DOWNLOAD]', e);
    res.status(500).json({ error: 'Failed to create ZIP', detail: e.message });
  }
});

// Fallback to React app
app.get('/{*path}', (req, res) => {
  const index = path.join(distPath, 'index.html');
  if (fs.existsSync(index)) res.sendFile(index);
  else res.json({ message: 'AI Tool Inspector API', docs: `${SERVICE_URL}/docs`, health: `${SERVICE_URL}/api/health` });
});

app.listen(PORT, () => {
  console.log(`\n🚀 AI Tool Inspector API [${SERVICE_NAME}]`);
  console.log(`   ${SERVICE_URL}/docs`);
  console.log(`   ${SERVICE_URL}/api/health`);
  console.log(`   ${SERVICE_URL}/api/summary\n`);
});

// ── Auto-start Telegram bot if token is set ───────────────────────────────────
if (TELEGRAM_BOT_TOKEN) {
  const botPath = path.join(__dirname, 'bot.mjs');
  if (fs.existsSync(botPath)) {
    console.log('🤖 Запускаю Telegram бота...');
    const bot = spawn(process.execPath, [botPath], {
      env: { ...process.env },
      stdio: 'inherit',
    });
    bot.on('exit', (code) => {
      console.log(`⚠️ Бот завершился с кодом ${code}, перезапускаю через 5 сек...`);
      setTimeout(() => {
        spawn(process.execPath, [botPath], { env: { ...process.env }, stdio: 'inherit' });
      }, 5000);
    });
  }
}
