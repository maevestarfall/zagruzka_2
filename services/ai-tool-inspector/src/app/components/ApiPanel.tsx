import { useState } from 'react';
import { Copy, Check, ExternalLink, Wifi, WifiOff, RefreshCw, Terminal } from 'lucide-react';
import { ConnectionStatus } from './useApi';

interface ApiPanelProps {
  status: ConnectionStatus;
  lastSync: string | null;
  apiBase: string;
  onRefresh: () => void;
}

const ENDPOINTS = [
  { method: 'GET', path: '/api/models', desc: 'Все модели со статистикой' },
  { method: 'GET', path: '/api/models/:id', desc: 'Полные данные модели + инструменты' },
  { method: 'GET', path: '/api/models/:id/tools', desc: 'Инструменты (фильтр ?status=)' },
  { method: 'GET', path: '/api/models/:id/tools/:toolId', desc: 'Один инструмент' },
  { method: 'POST', path: '/api/models/:id/tools/:toolId/verify', desc: 'Отправить результат проверки' },
  { method: 'POST', path: '/api/models/:id/tools', desc: 'Добавить инструмент' },
  { method: 'GET', path: '/api/summary', desc: 'Сводка: trustScore по всем' },
  { method: 'GET', path: '/api/models/:id/log', desc: 'Лог верификаций' },
  { method: 'GET', path: '/openapi.json', desc: 'OpenAPI спецификация' },
  { method: 'GET', path: '/docs', desc: 'Swagger UI' },
];

const methodColors: Record<string, string> = {
  GET: 'text-emerald-400 bg-emerald-400/10',
  POST: 'text-amber-400 bg-amber-400/10',
};

const statusConfig: Record<ConnectionStatus, { label: string; color: string; icon: React.ReactNode }> = {
  connecting: { label: 'Подключение...', color: 'text-yellow-400', icon: <RefreshCw className="w-3 h-3 animate-spin" /> },
  connected: { label: 'Подключён', color: 'text-emerald-400', icon: <Wifi className="w-3 h-3" /> },
  error: { label: 'Ошибка синхронизации', color: 'text-amber-400', icon: <Wifi className="w-3 h-3" /> },
  offline: { label: 'Сервер недоступен', color: 'text-red-400', icon: <WifiOff className="w-3 h-3" /> },
};

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <button onClick={copy} className="text-zinc-600 hover:text-zinc-400 transition-colors">
      {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
    </button>
  );
}

const EXAMPLE_VERIFY = `curl -X POST \\
  http://localhost:3001/api/models/claude-sonnet-4-6/tools/bash/verify \\
  -H "Content-Type: application/json" \\
  -d '{"result":"verified","message":"Протестировано","source":"my-ai"}'`;

export function ApiPanel({ status, lastSync, apiBase, onRefresh }: ApiPanelProps) {
  const [showExample, setShowExample] = useState(false);
  const cfg = statusConfig[status];

  return (
    <div className="border-t border-white/10 bg-[#0a0a0e]">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-white/10">
        <div className="flex items-center gap-2">
          <Terminal className="w-3.5 h-3.5 text-violet-400" />
          <span className="text-xs text-zinc-400 uppercase tracking-wider">API Сервер</span>
        </div>
        <div className="flex items-center gap-3">
          <span className={`flex items-center gap-1.5 text-xs ${cfg.color}`}>
            {cfg.icon}
            {cfg.label}
          </span>
          {lastSync && (
            <span className="text-xs text-zinc-700">
              {new Date(lastSync).toLocaleTimeString('ru-RU')}
            </span>
          )}
          <button onClick={onRefresh} className="text-zinc-600 hover:text-zinc-400">
            <RefreshCw className="w-3 h-3" />
          </button>
        </div>
      </div>

      <div className="px-4 py-3 flex gap-6 flex-wrap">
        {/* Base URL */}
        <div className="min-w-0">
          <p className="text-xs text-zinc-600 mb-1">Base URL</p>
          <div className="flex items-center gap-2 bg-zinc-900 border border-zinc-800 rounded px-2.5 py-1.5">
            <code className="text-xs text-violet-300">{apiBase}</code>
            <CopyButton text={apiBase} />
            <a
              href={`${apiBase}/docs`}
              target="_blank"
              rel="noreferrer"
              className="text-zinc-600 hover:text-zinc-400"
            >
              <ExternalLink className="w-3 h-3" />
            </a>
          </div>
        </div>

        {/* Endpoints */}
        <div className="flex-1 min-w-60">
          <p className="text-xs text-zinc-600 mb-1">Эндпоинты</p>
          <div className="space-y-1 max-h-36 overflow-y-auto pr-1">
            {ENDPOINTS.map((ep) => (
              <div key={ep.method + ep.path} className="flex items-center gap-2 group">
                <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 ${methodColors[ep.method]}`}>
                  {ep.method}
                </span>
                <code className="text-xs text-zinc-500 group-hover:text-zinc-300 transition-colors truncate">
                  {ep.path}
                </code>
                <span className="text-xs text-zinc-700 hidden group-hover:block truncate">
                  — {ep.desc}
                </span>
                <CopyButton text={`${apiBase}${ep.path}`} />
              </div>
            ))}
          </div>
        </div>

        {/* Example */}
        <div className="min-w-60 flex-1">
          <button
            onClick={() => setShowExample((v) => !v)}
            className="text-xs text-zinc-600 hover:text-zinc-400 transition-colors mb-1"
          >
            {showExample ? 'Скрыть пример' : 'Пример: отправить результат проверки ↓'}
          </button>
          {showExample && (
            <div className="relative bg-zinc-900 border border-zinc-800 rounded p-2.5">
              <pre className="text-[10px] text-zinc-400 overflow-x-auto whitespace-pre">{EXAMPLE_VERIFY}</pre>
              <div className="absolute top-2 right-2">
                <CopyButton text={EXAMPLE_VERIFY} />
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
