/**
 * AI Tool Inspector — Telegram Bot
 *
 * Команды:
 *   /start        — главное меню
 *   /stats        — статистика по всем сервисам
 *   /services     — список подключённых серверов
 *   /addserver    — добавить сервер по URL
 *   /removeserver — удалить сервер
 *   /models       — список ИИ-моделей (первый живой сервер)
 *   /link         — сгенерировать персональную ссылку для ИИ
 *   /tokens       — все активные токены
 *   /check        — проверить инструмент
 *   /help         — справка
 *
 * Переменные окружения:
 *   TELEGRAM_BOT_TOKEN  — токен бота от @BotFather (обязательно)
 *   DEFAULT_SERVER_URL  — URL первого сервера (по умолчанию http://localhost:3001)
 */

import { Telegraf, Markup } from 'telegraf';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
if (!BOT_TOKEN) {
  console.error('❌ TELEGRAM_BOT_TOKEN не задан. Установите переменную окружения.');
  process.exit(1);
}

const DATA_DIR = path.join(__dirname, 'bot-data');
const SERVICES_FILE = path.join(DATA_DIR, 'services.json');
fs.mkdirSync(DATA_DIR, { recursive: true });

// ─── Persistence ─────────────────────────────────────────────────────────────

function loadServices() {
  try { return JSON.parse(fs.readFileSync(SERVICES_FILE, 'utf8')); } catch { return { services: [] }; }
}
function saveServices(data) {
  fs.writeFileSync(SERVICES_FILE, JSON.stringify(data, null, 2));
}

function addService(url, name) {
  const data = loadServices();
  const id = `svc_${Date.now()}`;
  const existing = data.services.find(s => s.url === url);
  if (existing) return { added: false, service: existing };
  const svc = { id, url: url.replace(/\/$/, ''), name: name || url, addedAt: new Date().toISOString(), lastSeen: null, status: 'unknown' };
  data.services.push(svc);
  saveServices(data);
  return { added: true, service: svc };
}

function removeService(id) {
  const data = loadServices();
  const before = data.services.length;
  data.services = data.services.filter(s => s.id !== id);
  saveServices(data);
  return data.services.length < before;
}

// ─── API Helpers ──────────────────────────────────────────────────────────────

const DEFAULT_URL = (process.env.DEFAULT_SERVER_URL || 'http://localhost:3001').replace(/\/$/, '');

async function apiFetch(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const res = await fetch(url, { ...options, signal: controller.signal });
    clearTimeout(timeout);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (e) {
    clearTimeout(timeout);
    throw e;
  }
}

async function checkService(svc) {
  try {
    const data = await apiFetch(`${svc.url}/api/health`);
    return { online: true, data };
  } catch {
    return { online: false, data: null };
  }
}

async function getAllServicesWithStatus() {
  const data = loadServices();
  // Always include default server
  const allUrls = [DEFAULT_URL, ...data.services.map(s => s.url)];
  const unique = [...new Map(allUrls.map(u => [u, { id: u === DEFAULT_URL ? 'default' : data.services.find(s => s.url === u)?.id, url: u, name: u === DEFAULT_URL ? 'Default (local)' : data.services.find(s => s.url === u)?.name || u }])).values()];

  const results = await Promise.all(unique.map(async (svc) => {
    const { online, data: health } = await checkService(svc);
    return { ...svc, online, health };
  }));
  return results;
}

async function getFirstOnlineServer() {
  const services = await getAllServicesWithStatus();
  return services.find(s => s.online) || null;
}

// ─── Bot ─────────────────────────────────────────────────────────────────────

const bot = new Telegraf(BOT_TOKEN);

// Main menu keyboard
const mainMenu = Markup.keyboard([
  ['📊 Статистика', '🌐 Сервисы'],
  ['🤖 Модели', '🔗 Ссылки для ИИ'],
  ['🔑 Токены', '❓ Помощь'],
]).resize();

// ─── /start ──────────────────────────────────────────────────────────────────

bot.start(async (ctx) => {
  await ctx.reply(
    `🤖 <b>AI Tool Inspector Bot</b>\n\n` +
    `Я управляю системой верификации инструментов ИИ-агентов.\n\n` +
    `<b>Что умею:</b>\n` +
    `📊 Показывать статистику по всем серверам\n` +
    `🌐 Управлять подключёнными серверами\n` +
    `🔗 Генерировать уникальные ссылки для каждого ИИ\n` +
    `✅ Принимать результаты верификации инструментов\n\n` +
    `Выбери действие из меню ниже:`,
    { parse_mode: 'HTML', ...mainMenu }
  );
});

// ─── /help ───────────────────────────────────────────────────────────────────

bot.help(async (ctx) => {
  await ctx.reply(
    `<b>📖 Справка</b>\n\n` +
    `<b>Команды:</b>\n` +
    `/start — главное меню\n` +
    `/stats — статистика всех серверов\n` +
    `/services — список серверов\n` +
    `/addserver &lt;url&gt; [название] — добавить сервер\n` +
    `/removeserver &lt;id&gt; — удалить сервер\n` +
    `/models — список ИИ-моделей\n` +
    `/link &lt;model-id&gt; — ссылка для ИИ\n` +
    `/tokens — активные токены\n` +
    `/check &lt;model&gt; &lt;tool&gt; — проверить инструмент\n\n` +
    `<b>Пример добавления Render сервера:</b>\n` +
    `<code>/addserver https://my-app.onrender.com Render</code>\n\n` +
    `<b>Пример генерации ссылки для GPT-4o:</b>\n` +
    `<code>/link gpt-4o</code>`,
    { parse_mode: 'HTML' }
  );
});

// ─── 📊 Статистика ────────────────────────────────────────────────────────────

async function sendStats(ctx) {
  const msg = await ctx.reply('⏳ Собираю статистику со всех серверов...');
  const services = await getAllServicesWithStatus();

  if (services.length === 0) {
    return ctx.telegram.editMessageText(msg.chat.id, msg.message_id, null,
      '❌ Нет подключённых серверов. Добавь через /addserver');
  }

  let text = `📊 <b>Статистика AI Tool Inspector</b>\n` +
    `🕐 ${new Date().toLocaleString('ru-RU')}\n\n`;

  let totalVerified = 0, totalFailed = 0, totalTools = 0, onlineCount = 0;

  for (const svc of services) {
    if (!svc.online) {
      text += `❌ <b>${svc.name}</b>\n<i>Недоступен</i>\n\n`;
      continue;
    }
    onlineCount++;
    const h = svc.health;
    text += `✅ <b>${svc.name || svc.url}</b>\n`;
    text += `🔗 <code>${svc.url}</code>\n`;
    text += `⏱ Uptime: ${Math.floor(h.uptime / 60)} мин\n`;
    text += `📦 Моделей: ${h.models} | Инструментов: ${h.totalTools}\n`;

    if (h.stats) {
      for (const m of h.stats) {
        const bar = m.verified > 0
          ? '✅'.repeat(Math.min(m.verified, 5)) + (m.failed > 0 ? '❌'.repeat(Math.min(m.failed, 3)) : '')
          : '⬜';
        text += `  ${bar} ${m.name}: ${m.verified}✅ ${m.failed}❌ / ${m.total}\n`;
        totalVerified += m.verified;
        totalFailed += m.failed;
        totalTools += m.total;
      }
    }
    text += '\n';
  }

  text += `━━━━━━━━━━━━━━━━━\n`;
  text += `🌐 Серверов онлайн: ${onlineCount}/${services.length}\n`;
  text += `📊 Итого: ${totalVerified}✅ ${totalFailed}❌ из ${totalTools} инструментов\n`;
  if (totalVerified + totalFailed > 0) {
    const score = Math.round(totalVerified / (totalVerified + totalFailed) * 100);
    const bar = '█'.repeat(Math.floor(score / 10)) + '░'.repeat(10 - Math.floor(score / 10));
    text += `📈 Достоверность: [${bar}] ${score}%`;
  }

  await ctx.telegram.editMessageText(msg.chat.id, msg.message_id, null, text, {
    parse_mode: 'HTML',
    ...Markup.inlineKeyboard([[Markup.button.callback('🔄 Обновить', 'refresh_stats')]]),
  });
}

bot.command('stats', sendStats);
bot.hears('📊 Статистика', sendStats);
bot.action('refresh_stats', async (ctx) => { await ctx.answerCbQuery(); await sendStats(ctx); });

// ─── 🌐 Сервисы ───────────────────────────────────────────────────────────────

async function sendServices(ctx) {
  const services = await getAllServicesWithStatus();
  const data = loadServices();

  let text = `🌐 <b>Подключённые серверы</b>\n\n`;
  const buttons = [];

  // Default
  const def = services.find(s => s.id === 'default');
  text += `${def?.online ? '🟢' : '🔴'} <b>Default (local)</b>\n`;
  text += `<code>${DEFAULT_URL}</code>\n`;
  text += def?.online ? `✅ Онлайн | ${def.health.models} моделей\n` : `❌ Недоступен\n`;
  text += '\n';

  // User-added
  for (const svc of data.services) {
    const live = services.find(s => s.url === svc.url);
    text += `${live?.online ? '🟢' : '🔴'} <b>${svc.name}</b>\n`;
    text += `<code>${svc.url}</code>\n`;
    text += `ID: <code>${svc.id}</code>\n`;
    text += live?.online ? `✅ Онлайн | ${live.health.models} моделей | uptime ${Math.floor(live.health.uptime / 60)} мин\n` : `❌ Недоступен\n`;
    text += `Добавлен: ${new Date(svc.addedAt).toLocaleDateString('ru-RU')}\n\n`;
    buttons.push([Markup.button.callback(`🗑 Удалить ${svc.name}`, `remove_svc_${svc.id}`)]);
  }

  text += `━━━━━━━━━━━━━━━━━\n`;
  text += `Всего: ${data.services.length + 1} серверов | Онлайн: ${services.filter(s => s.online).length}\n\n`;
  text += `Добавить: <code>/addserver https://your-app.onrender.com Render</code>`;

  await ctx.reply(text, {
    parse_mode: 'HTML',
    ...Markup.inlineKeyboard([
      [Markup.button.callback('🔄 Обновить', 'refresh_services')],
      ...buttons,
    ]),
  });
}

bot.command('services', sendServices);
bot.hears('🌐 Сервисы', sendServices);
bot.action('refresh_services', async (ctx) => { await ctx.answerCbQuery(); await sendServices(ctx); });

bot.action(/remove_svc_(.+)/, async (ctx) => {
  const id = ctx.match[1];
  const removed = removeService(id);
  await ctx.answerCbQuery(removed ? '✅ Удалено' : '❌ Не найдено');
  await sendServices(ctx);
});

// ─── /addserver ───────────────────────────────────────────────────────────────

bot.command('addserver', async (ctx) => {
  const args = ctx.message.text.split(' ').slice(1);
  if (!args[0]) {
    return ctx.reply('Использование: <code>/addserver https://your-app.onrender.com [Название]</code>', { parse_mode: 'HTML' });
  }
  const url = args[0];
  const name = args.slice(1).join(' ') || url;

  if (!url.startsWith('http')) {
    return ctx.reply('❌ URL должен начинаться с http:// или https://');
  }

  const msg = await ctx.reply(`⏳ Проверяю сервер ${url}...`);

  try {
    const health = await apiFetch(`${url.replace(/\/$/, '')}/api/health`);
    const { added, service } = addService(url, name);

    await ctx.telegram.editMessageText(msg.chat.id, msg.message_id, null,
      `${added ? '✅ Сервер добавлен' : '⚠️ Сервер уже существует'}\n\n` +
      `<b>${service.name}</b>\n` +
      `🔗 <code>${service.url}</code>\n` +
      `📦 Моделей: ${health.models} | Инструментов: ${health.totalTools}\n` +
      `⏱ Uptime: ${Math.floor(health.uptime / 60)} мин\n` +
      `ID: <code>${service.id}</code>`,
      { parse_mode: 'HTML' }
    );
  } catch {
    await ctx.telegram.editMessageText(msg.chat.id, msg.message_id, null,
      `⚠️ Сервер недоступен, но добавлен в список (возможно спит на Render).\n<code>${url}</code>`,
      { parse_mode: 'HTML' }
    );
    addService(url, name);
  }
});

// ─── /removeserver ────────────────────────────────────────────────────────────

bot.command('removeserver', async (ctx) => {
  const id = ctx.message.text.split(' ')[1];
  if (!id) return ctx.reply('Использование: /removeserver <id>\nПосмотри ID через /services');
  const removed = removeService(id);
  await ctx.reply(removed ? `✅ Сервер ${id} удалён` : `❌ Сервер с ID ${id} не найден`);
});

// ─── 🤖 Модели ────────────────────────────────────────────────────────────────

async function sendModels(ctx) {
  const svc = await getFirstOnlineServer();
  if (!svc) return ctx.reply('❌ Нет доступных серверов. Добавь через /addserver');

  try {
    const { models } = await apiFetch(`${svc.url}/api/models`);
    let text = `🤖 <b>ИИ-модели</b> (${svc.name})\n\n`;
    const buttons = [];

    for (const m of models) {
      const score = m.stats.verified + m.stats.failed > 0
        ? Math.round(m.stats.verified / (m.stats.verified + m.stats.failed) * 100) + '%'
        : '—';
      text += `<b>${m.name}</b> <i>(${m.provider})</i>\n`;
      text += `  ✅ ${m.stats.verified}  ❌ ${m.stats.failed}  ⬜ ${m.stats.unchecked}  |  ${score} достоверность\n`;
      text += `  ID: <code>${m.id}</code>\n\n`;
      buttons.push([
        Markup.button.callback(`🔗 Ссылка для ${m.name}`, `gen_link_${m.id}`),
        Markup.button.callback(`✅ Проверить все`, `verify_all_${m.id}`),
      ]);
    }

    await ctx.reply(text, { parse_mode: 'HTML', ...Markup.inlineKeyboard(buttons) });
  } catch (e) {
    await ctx.reply(`❌ Ошибка: ${e.message}`);
  }
}

bot.command('models', sendModels);
bot.hears('🤖 Модели', sendModels);

// ─── 🔗 Генерация ссылок для ИИ ──────────────────────────────────────────────

async function genLink(ctx, modelId) {
  const svc = await getFirstOnlineServer();
  if (!svc) return ctx.reply('❌ Нет доступных серверов');

  const msg = await ctx.reply('⏳ Генерирую токен...');
  try {
    const data = await apiFetch(`${svc.url}/api/tokens/${modelId}`, { method: 'POST' });

    const text =
      `🔗 <b>Персональная ссылка для ${data.modelName}</b>\n\n` +
      `<b>Токен:</b> <code>${data.token}</code>\n\n` +
      `<b>📥 Получить задания (что проверить):</b>\n<code>GET ${data.probeUrl}</code>\n\n` +
      `<b>📤 Отправить результат:</b>\n<code>POST ${data.verifyUrl}</code>\n\n` +
      `<b>Тело запроса:</b>\n` +
      `<code>{\n  "toolId": "bash",\n  "result": "verified",\n  "message": "Протестировано"\n}</code>\n\n` +
      `<i>Скопируй это в промпт для ${data.modelName} — он сам вызовет API и отправит результаты.</i>`;

    await ctx.telegram.editMessageText(msg.chat.id, msg.message_id, null, text, {
      parse_mode: 'HTML',
      ...Markup.inlineKeyboard([[Markup.button.callback('🔄 Перегенерировать', `gen_link_${modelId}`)]]),
    });
  } catch (e) {
    await ctx.telegram.editMessageText(msg.chat.id, msg.message_id, null, `❌ Ошибка: ${e.message}`);
  }
}

async function sendLinksMenu(ctx) {
  const svc = await getFirstOnlineServer();
  if (!svc) return ctx.reply('❌ Нет доступных серверов. Добавь через /addserver');
  try {
    const { models } = await apiFetch(`${svc.url}/api/models`);
    await ctx.reply(
      '🔗 <b>Выбери ИИ для генерации ссылки:</b>\n<i>Каждый получит уникальный токен доступа</i>',
      {
        parse_mode: 'HTML',
        ...Markup.inlineKeyboard(models.map(m => [Markup.button.callback(`${m.name} (${m.provider})`, `gen_link_${m.id}`)])),
      }
    );
  } catch (e) {
    await ctx.reply(`❌ Ошибка: ${e.message}`);
  }
}

bot.command('link', async (ctx) => {
  const modelId = ctx.message.text.split(' ')[1];
  if (modelId) await genLink(ctx, modelId);
  else await sendLinksMenu(ctx);
});
bot.hears('🔗 Ссылки для ИИ', sendLinksMenu);
bot.action(/gen_link_(.+)/, async (ctx) => { await ctx.answerCbQuery(); await genLink(ctx, ctx.match[1]); });

// ─── 🔑 Токены ────────────────────────────────────────────────────────────────

async function sendTokens(ctx) {
  const svc = await getFirstOnlineServer();
  if (!svc) return ctx.reply('❌ Нет доступных серверов');
  try {
    const { tokens } = await apiFetch(`${svc.url}/api/tokens`);
    if (tokens.length === 0) {
      return ctx.reply('🔑 Токенов нет. Сгенерируй через /link или кнопку "Ссылки для ИИ"');
    }
    let text = `🔑 <b>Активные токены</b> (${svc.name})\n\n`;
    for (const t of tokens) {
      text += `<b>${t.modelId}</b>\n`;
      text += `Токен: <code>${t.token}</code>\n`;
      text += `Создан: ${new Date(t.createdAt).toLocaleString('ru-RU')}\n`;
      text += `Probe URL: <code>${t.probeUrl}</code>\n\n`;
    }
    await ctx.reply(text, { parse_mode: 'HTML' });
  } catch (e) {
    await ctx.reply(`❌ Ошибка: ${e.message}`);
  }
}

bot.command('tokens', sendTokens);
bot.hears('🔑 Токены', sendTokens);

// ─── ✅ Verify all (inline) ───────────────────────────────────────────────────

bot.action(/verify_all_(.+)/, async (ctx) => {
  await ctx.answerCbQuery('⏳ Запускаю проверку...');
  const modelId = ctx.match[1];
  const svc = await getFirstOnlineServer();
  if (!svc) return ctx.reply('❌ Нет доступных серверов');

  try {
    const { tools } = await apiFetch(`${svc.url}/api/models/${modelId}/tools`);
    await ctx.reply(`⏳ Проверяю ${tools.length} инструментов для <b>${modelId}</b>...`, { parse_mode: 'HTML' });

    let verified = 0, failed = 0;
    for (const tool of tools) {
      if (tool.verificationStatus !== 'unchecked') { verified++; continue; }
      const result = tool.reallyExists === false ? 'failed' : 'verified';
      try {
        await apiFetch(`${svc.url}/api/models/${modelId}/tools/${tool.id}/verify`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ result, message: 'Запущено из Telegram бота', source: 'telegram-bot' }),
        });
        if (result === 'verified') verified++; else failed++;
      } catch { failed++; }
      await new Promise(r => setTimeout(r, 300));
    }

    await ctx.reply(
      `✅ Проверка завершена для <b>${modelId}</b>\n` +
      `✅ Подтверждено: ${verified}\n❌ Не существует: ${failed}`,
      { parse_mode: 'HTML' }
    );
  } catch (e) {
    await ctx.reply(`❌ Ошибка: ${e.message}`);
  }
});

// ─── /check ───────────────────────────────────────────────────────────────────

bot.command('check', async (ctx) => {
  const [, modelId, toolId] = ctx.message.text.split(' ');
  if (!modelId || !toolId) {
    return ctx.reply('Использование: <code>/check &lt;model-id&gt; &lt;tool-id&gt;</code>\nПример: <code>/check claude-sonnet-4-6 bash</code>', { parse_mode: 'HTML' });
  }
  const svc = await getFirstOnlineServer();
  if (!svc) return ctx.reply('❌ Нет доступных серверов');

  try {
    const tool = await apiFetch(`${svc.url}/api/models/${modelId}/tools/${toolId}`);
    const statusMap = { verified: '✅ Подтверждён', failed: '❌ Не существует', unchecked: '⬜ Не проверен', pending: '⏳ Проверяется' };
    await ctx.reply(
      `🔍 <b>${tool.name}</b>\n` +
      `Модель: ${modelId}\n` +
      `Статус: ${statusMap[tool.verificationStatus] || tool.verificationStatus}\n` +
      `Категория: ${tool.category}\n` +
      `Описание: ${tool.description}\n` +
      (tool.notes ? `Заметки: <i>${tool.notes}</i>\n` : '') +
      (tool.verifiedAt ? `Проверено: ${new Date(tool.verifiedAt).toLocaleString('ru-RU')}` : ''),
      {
        parse_mode: 'HTML',
        ...Markup.inlineKeyboard([
          [
            Markup.button.callback('✅ Подтвердить', `quick_verify_${modelId}_${toolId}_verified`),
            Markup.button.callback('❌ Не существует', `quick_verify_${modelId}_${toolId}_failed`),
          ],
        ]),
      }
    );
  } catch (e) {
    await ctx.reply(`❌ Ошибка: ${e.message}`);
  }
});

bot.action(/quick_verify_(.+)_(.+)_(verified|failed)/, async (ctx) => {
  await ctx.answerCbQuery('⏳...');
  const [modelId, toolId, result] = [ctx.match[1], ctx.match[2], ctx.match[3]];
  const svc = await getFirstOnlineServer();
  if (!svc) return;
  try {
    await apiFetch(`${svc.url}/api/models/${modelId}/tools/${toolId}/verify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ result, message: 'Верификация из Telegram', source: 'telegram-user' }),
    });
    await ctx.answerCbQuery(result === 'verified' ? '✅ Подтверждено!' : '❌ Отмечено как несуществующее');
    await ctx.reply(`${result === 'verified' ? '✅' : '❌'} <b>${toolId}</b> (${modelId}) — ${result === 'verified' ? 'подтверждён' : 'не существует'}`, { parse_mode: 'HTML' });
  } catch (e) {
    await ctx.reply(`❌ Ошибка: ${e.message}`);
  }
});

// ─── Polling (авто-статистика каждые 30 мин) ──────────────────────────────────

let adminChatId = process.env.TELEGRAM_CHAT_ID || null;

bot.on('message', async (ctx, next) => {
  if (!adminChatId) adminChatId = String(ctx.chat.id);
  return next();
});

async function scheduledReport() {
  if (!adminChatId) return;
  const services = await getAllServicesWithStatus();
  const online = services.filter(s => s.online).length;
  const total = services.length;

  if (online === 0) {
    await bot.telegram.sendMessage(adminChatId,
      `⚠️ <b>Плановый отчёт</b>\n🔴 Все серверы недоступны (${total} серверов)`,
      { parse_mode: 'HTML' }
    );
    return;
  }

  let totalV = 0, totalF = 0, totalT = 0;
  for (const s of services.filter(s => s.online)) {
    totalV += s.health.stats.reduce((a, m) => a + m.verified, 0);
    totalF += s.health.stats.reduce((a, m) => a + m.failed, 0);
    totalT += s.health.totalTools;
  }
  const score = totalV + totalF > 0 ? Math.round(totalV / (totalV + totalF) * 100) : 0;

  await bot.telegram.sendMessage(adminChatId,
    `📊 <b>Плановый отчёт</b> — ${new Date().toLocaleString('ru-RU')}\n\n` +
    `🌐 Серверов онлайн: ${online}/${total}\n` +
    `✅ Подтверждено: ${totalV} | ❌ Нет: ${totalF} | Всего: ${totalT}\n` +
    `📈 Достоверность: ${score}%`,
    { parse_mode: 'HTML' }
  );
}

// Отчёт каждые 30 минут
setInterval(scheduledReport, 30 * 60 * 1000);

// ─── Launch ───────────────────────────────────────────────────────────────────

bot.launch(() => {
  console.log('\n🤖 Telegram Bot запущен');
  console.log('   Открой бота и отправь /start\n');
});

process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));
