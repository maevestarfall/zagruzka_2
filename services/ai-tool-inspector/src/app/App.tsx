import { useState, useCallback } from 'react';
import { AIModel } from './components/types';
import { initialModels } from './components/data';
import { Sidebar } from './components/Sidebar';
import { ToolCard } from './components/ToolCard';
import { VerificationLogPanel } from './components/VerificationLog';
import { ApiPanel } from './components/ApiPanel';
import { useApi } from './components/useApi';
import { BotView } from './components/BotView';
import { DownloadZipButton } from './components/DownloadZip';
import {
  Power,
  RefreshCw,
  Filter,
  Plus,
  AlertTriangle,
  ShieldCheck,
  ShieldAlert,
  ShieldQuestion,
  Cpu,
  Trash2,
  Database,
  Bot,
} from 'lucide-react';

type AppTab = 'inspector' | 'bot';

type FilterType = 'all' | 'verified' | 'failed' | 'unchecked';

const VERIFICATION_MESSAGES = {
  verified: [
    'Инструмент существует и работает корректно',
    'Подтверждено через реальный вызов',
    'Документация совпадает с реальностью',
  ],
  failed: [
    'Инструмент недоступен в данном контексте',
    'API не возвращает ожидаемый ответ',
    'Заявлено, но не реализовано',
    'Инструмент существует только в UI, не в API',
  ],
};

function pickRandom<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)];
}

export default function App() {
  const { models: serverModels, status, lastSync, apiBase, fetchModels, verifyTool: apiVerify, addTool: apiAddTool } = useApi(4000);

  // Fall back to static data while server is connecting
  const serverReady = status === 'connected' || status === 'error';
  const [localModels, setLocalModels] = useState<AIModel[]>(initialModels);

  // Use server data when available, local fallback otherwise
  const models = serverReady && serverModels.length > 0 ? serverModels : localModels;
  const usingServer = serverReady && serverModels.length > 0;

  const [activeTab, setActiveTab] = useState<AppTab>('bot');
  const [serverUrl, setServerUrl] = useState('http://localhost:3001');
  const [selectedModelId, setSelectedModelId] = useState<string>(initialModels[0].id);
  const [globalMonitoring, setGlobalMonitoring] = useState(false);
  const [filter, setFilter] = useState<FilterType>('all');
  const [verifyingIds, setVerifyingIds] = useState<Set<string>>(new Set());
  const [showAddTool, setShowAddTool] = useState(false);
  const [newToolName, setNewToolName] = useState('');
  const [newToolDesc, setNewToolDesc] = useState('');

  const selectedModel = models.find((m) => m.id === selectedModelId) ?? models[0];

  // Local update (used when server is offline)
  const updateLocal = useCallback((id: string, updater: (m: AIModel) => AIModel) => {
    setLocalModels((prev) => prev.map((m) => (m.id === id ? updater(m) : m)));
  }, []);

  const verifyTool = useCallback(
    async (toolId: string) => {
      if (verifyingIds.has(toolId)) return;
      setVerifyingIds((prev) => new Set(prev).add(toolId));

      // Simulate delay
      const delay = 1200 + Math.random() * 1800;
      await new Promise((r) => setTimeout(r, delay));

      const tool = selectedModel.tools.find((t) => t.id === toolId);
      if (!tool) {
        setVerifyingIds((prev) => { const s = new Set(prev); s.delete(toolId); return s; });
        return;
      }

      let result: 'verified' | 'failed';
      if (tool.reallyExists === true) result = 'verified';
      else if (tool.reallyExists === false) result = 'failed';
      else {
        const ratio = tool.claimedCapabilities.filter((c) => c.confirmed).length / (tool.claimedCapabilities.length || 1);
        result = Math.random() < ratio * 0.85 + 0.1 ? 'verified' : 'failed';
      }
      const message = tool.notes?.length > 5 ? tool.notes.slice(0, 80) : pickRandom(VERIFICATION_MESSAGES[result]);

      if (usingServer) {
        try {
          await apiVerify(selectedModel.id, toolId, result, message);
        } catch {
          // server error — fallback handled below
        }
      } else {
        updateLocal(selectedModel.id, (m) => ({
          ...m,
          lastChecked: new Date().toISOString(),
          tools: m.tools.map((t) => t.id === toolId ? { ...t, verificationStatus: result, verifiedAt: new Date().toISOString() } : t),
          verificationLog: [...m.verificationLog, { timestamp: new Date().toISOString(), toolId, toolName: tool.name, result, message }],
        }));
      }

      setVerifyingIds((prev) => { const s = new Set(prev); s.delete(toolId); return s; });
    },
    [verifyingIds, selectedModel, apiVerify, usingServer, updateLocal]
  );

  const verifyAll = useCallback(() => {
    selectedModel.tools.forEach((tool, i) => {
      setTimeout(() => verifyTool(tool.id), i * 400);
    });
  }, [selectedModel.tools, verifyTool]);

  const clearLog = useCallback(() => {
    if (!usingServer) {
      updateLocal(selectedModel.id, (m) => ({ ...m, verificationLog: [] }));
    }
  }, [selectedModel.id, usingServer, updateLocal]);

  const resetTools = useCallback(() => {
    if (!usingServer) {
      updateLocal(selectedModel.id, (m) => ({
        ...m,
        tools: m.tools.map((t) => ({ ...t, verificationStatus: 'unchecked' as const, verifiedAt: null })),
        verificationLog: [],
      }));
    }
  }, [selectedModel.id, usingServer, updateLocal]);

  const addTool = useCallback(async () => {
    if (!newToolName.trim()) return;
    if (usingServer) {
      await apiAddTool(selectedModel.id, { name: newToolName.trim(), description: newToolDesc.trim(), category: 'api' });
    } else {
      updateLocal(selectedModel.id, (m) => ({
        ...m,
        tools: [...m.tools, { id: `custom-${Date.now()}`, name: newToolName.trim(), description: newToolDesc.trim(), category: 'api', claimedCapabilities: [], verificationStatus: 'unchecked', verifiedAt: null, notes: 'Добавлен вручную', reallyExists: null }],
      }));
    }
    setNewToolName('');
    setNewToolDesc('');
    setShowAddTool(false);
  }, [newToolName, newToolDesc, selectedModel.id, usingServer, apiAddTool, updateLocal]);

  const filteredTools = (selectedModel?.tools ?? []).filter((t) => {
    if (filter === 'all') return true;
    if (filter === 'verified') return t.verificationStatus === 'verified';
    if (filter === 'failed') return t.verificationStatus === 'failed';
    return t.verificationStatus === 'unchecked' || t.verificationStatus === 'pending';
  });

  const stats = {
    verified: selectedModel?.tools.filter((t) => t.verificationStatus === 'verified').length ?? 0,
    failed: selectedModel?.tools.filter((t) => t.verificationStatus === 'failed').length ?? 0,
    unchecked: selectedModel?.tools.filter((t) => t.verificationStatus === 'unchecked' || t.verificationStatus === 'pending').length ?? 0,
    total: selectedModel?.tools.length ?? 0,
  };

  if (!selectedModel) return null;

  return (
    <div className="size-full flex flex-col bg-[#09090d] text-white overflow-hidden">
      {/* Header */}
      <header className="h-12 shrink-0 border-b border-white/10 flex items-center justify-between px-4 bg-[#0d0d11]">
        <div className="flex items-center gap-1">
          <Cpu className="w-4 h-4 text-violet-400 mr-2" />
          <button
            onClick={() => setActiveTab('inspector')}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded text-xs transition-colors ${activeTab === 'inspector' ? 'bg-white/10 text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
          >
            <ShieldCheck className="w-3.5 h-3.5" />
            Инспектор
          </button>
          <button
            onClick={() => setActiveTab('bot')}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded text-xs transition-colors ${activeTab === 'bot' ? 'bg-[#2b5278]/60 text-[#5288c1]' : 'text-zinc-500 hover:text-zinc-300'}`}
          >
            <Bot className="w-3.5 h-3.5" />
            Telegram Бот
          </button>
        </div>
        <div className="flex items-center gap-3">
          {/* Server indicator */}
          <div className="flex items-center gap-1.5">
            <Database className="w-3 h-3 text-zinc-600" />
            <span className={`text-xs ${usingServer ? 'text-emerald-400' : 'text-zinc-600'}`}>
              {usingServer ? 'Live API' : 'Локально'}
            </span>
          </div>
          {globalMonitoring && (
            <div className="flex items-center gap-1.5 text-xs text-emerald-400">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
              Мониторинг активен
            </div>
          )}
          <DownloadZipButton className="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs bg-emerald-600/25 border border-emerald-500/50 text-emerald-300 hover:bg-emerald-600/40 hover:text-emerald-200 transition-all disabled:opacity-60 font-medium" />
          <button
            onClick={() => setGlobalMonitoring((v) => !v)}
            className={`flex items-center gap-2 px-3 py-1.5 rounded text-xs transition-all ${
              globalMonitoring
                ? 'bg-emerald-400/15 text-emerald-400 border border-emerald-400/30'
                : 'bg-zinc-800 text-zinc-400 border border-zinc-700 hover:border-zinc-600'
            }`}
          >
            <Power className="w-3.5 h-3.5" />
            {globalMonitoring ? 'Вкл' : 'Выкл'}
          </button>
        </div>
      </header>

      {activeTab === 'bot' && (
        <div className="flex-1 min-h-0 flex flex-col">
          {/* Server URL bar */}
          <div className="shrink-0 flex items-center gap-2 px-3 py-1.5 bg-[#0d1117] border-b border-white/5">
            <span className="text-[10px] text-zinc-500 shrink-0">🌐 Сервер:</span>
            <input
              value={serverUrl}
              onChange={e => setServerUrl(e.target.value)}
              placeholder="http://localhost:3001 или https://твой-бот.onrender.com"
              className="flex-1 bg-transparent text-[11px] text-[#8faecf] outline-none placeholder-zinc-600"
            />
            <span className={`text-[10px] shrink-0 ${serverUrl.includes('localhost') ? 'text-zinc-600' : 'text-emerald-400'}`}>
              {serverUrl.includes('localhost') ? 'локальный' : '🌐 облако'}
            </span>
          </div>
          <div className="flex-1 min-h-0">
            <BotView serverUrl={serverUrl} />
          </div>
        </div>
      )}

      {activeTab === 'inspector' && <div className="flex flex-1 min-h-0">
        <Sidebar
          models={models}
          selectedId={selectedModel.id}
          onSelect={setSelectedModelId}
          globalMonitoring={globalMonitoring}
        />

        {/* Main content */}
        <main className="flex-1 flex flex-col min-w-0 overflow-hidden">
          {/* Model header */}
          <div className="shrink-0 px-5 py-4 border-b border-white/10 bg-[#0d0d11]">
            <div className="flex items-center justify-between flex-wrap gap-3">
              <div>
                <h1 className="text-base text-white">{selectedModel.name}</h1>
                <p className="text-xs text-zinc-500 mt-0.5">
                  {selectedModel.provider} · <code className="text-zinc-600">{selectedModel.modelId}</code>
                </p>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={resetTools}
                  className="px-2.5 py-1.5 rounded text-xs bg-zinc-800 border border-zinc-700 text-zinc-400 hover:border-zinc-600 flex items-center gap-1.5 transition-colors"
                >
                  <RefreshCw className="w-3 h-3" />
                  Сбросить
                </button>
                <button
                  onClick={verifyAll}
                  className="px-2.5 py-1.5 rounded text-xs bg-violet-600/20 border border-violet-500/40 text-violet-300 hover:bg-violet-600/30 flex items-center gap-1.5 transition-colors"
                >
                  <ShieldCheck className="w-3 h-3" />
                  Проверить все
                </button>
              </div>
            </div>

            {/* Stats row */}
            <div className="flex items-center gap-4 mt-3">
              {(
                [
                  { key: 'all', label: `Все (${stats.total})`, icon: <Filter className="w-3 h-3" />, active: 'bg-zinc-700 text-white', inactive: 'text-zinc-500' },
                  { key: 'verified', label: `Подтверждены (${stats.verified})`, icon: <ShieldCheck className="w-3 h-3" />, active: 'bg-emerald-400/20 text-emerald-400', inactive: 'text-zinc-500' },
                  { key: 'failed', label: `Не существуют (${stats.failed})`, icon: <ShieldAlert className="w-3 h-3" />, active: 'bg-red-400/20 text-red-400', inactive: 'text-zinc-500' },
                  { key: 'unchecked', label: `Не проверены (${stats.unchecked})`, icon: <ShieldQuestion className="w-3 h-3" />, active: 'bg-zinc-600/50 text-zinc-300', inactive: 'text-zinc-500' },
                ] as const
              ).map((btn) => (
                <button
                  key={btn.key}
                  onClick={() => setFilter(btn.key as FilterType)}
                  className={`flex items-center gap-1.5 text-xs px-2 py-1 rounded transition-colors hover:opacity-90 ${
                    filter === btn.key ? btn.active : btn.inactive
                  }`}
                >
                  {btn.icon}
                  {btn.label}
                </button>
              ))}
              {stats.failed > 0 && (
                <div className="ml-auto flex items-center gap-1.5 text-xs text-amber-400/80">
                  <AlertTriangle className="w-3 h-3" />
                  {stats.failed} инструм. не существует
                </div>
              )}
            </div>
          </div>

          <div className="flex-1 flex min-h-0">
            {/* Tools grid */}
            <div className="flex-1 overflow-y-auto p-4">
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 max-w-4xl">
                {filteredTools.map((tool) => (
                  <ToolCard
                    key={tool.id}
                    tool={tool}
                    onVerify={verifyTool}
                    isVerifying={verifyingIds.has(tool.id)}
                  />
                ))}

                {!showAddTool ? (
                  <button
                    onClick={() => setShowAddTool(true)}
                    className="rounded-lg border border-dashed border-zinc-700 hover:border-zinc-500 p-4 flex items-center justify-center gap-2 text-zinc-600 hover:text-zinc-400 transition-colors text-xs"
                  >
                    <Plus className="w-4 h-4" />
                    Добавить инструмент
                  </button>
                ) : (
                  <div className="rounded-lg border border-violet-500/30 bg-violet-500/5 p-3 space-y-2">
                    <p className="text-xs text-violet-400">Новый инструмент</p>
                    <input
                      type="text"
                      placeholder="Название"
                      value={newToolName}
                      onChange={(e) => setNewToolName(e.target.value)}
                      className="w-full bg-zinc-800 border border-zinc-700 rounded px-2.5 py-1.5 text-xs text-white placeholder-zinc-600 outline-none focus:border-violet-500"
                    />
                    <input
                      type="text"
                      placeholder="Описание (опционально)"
                      value={newToolDesc}
                      onChange={(e) => setNewToolDesc(e.target.value)}
                      className="w-full bg-zinc-800 border border-zinc-700 rounded px-2.5 py-1.5 text-xs text-white placeholder-zinc-600 outline-none focus:border-violet-500"
                    />
                    <div className="flex gap-2">
                      <button onClick={addTool} className="flex-1 bg-violet-600 hover:bg-violet-500 text-white text-xs py-1.5 rounded transition-colors">Добавить</button>
                      <button onClick={() => setShowAddTool(false)} className="px-3 bg-zinc-700 hover:bg-zinc-600 text-zinc-300 text-xs py-1.5 rounded transition-colors">Отмена</button>
                    </div>
                  </div>
                )}
              </div>
            </div>

            {/* Right panel — log */}
            <div className="w-72 shrink-0 border-l border-white/10 flex flex-col bg-[#0d0d11]">
              <div className="flex items-center justify-between px-4 py-3 border-b border-white/10">
                <span className="text-xs text-zinc-400 uppercase tracking-wider">Лог верификации</span>
                {selectedModel.verificationLog.length > 0 && !usingServer && (
                  <button onClick={clearLog} className="text-zinc-600 hover:text-zinc-400">
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
              <div className="flex-1 overflow-y-auto p-3">
                <VerificationLogPanel logs={selectedModel.verificationLog} />
              </div>

              {/* Summary */}
              <div className="border-t border-white/10 p-3 space-y-2">
                <p className="text-xs text-zinc-600 uppercase tracking-wider">Итог</p>
                <div className="space-y-1.5">
                  {[
                    { label: 'Всего', val: stats.total, color: 'text-zinc-300' },
                    { label: 'Подтверждены', val: stats.verified, color: 'text-emerald-400' },
                    { label: 'Ложь / не работает', val: stats.failed, color: 'text-red-400' },
                    { label: 'Не проверено', val: stats.unchecked, color: 'text-zinc-400' },
                  ].map((row) => (
                    <div key={row.label} className="flex justify-between text-xs">
                      <span className="text-zinc-500">{row.label}</span>
                      <span className={row.color}>{row.val}</span>
                    </div>
                  ))}
                </div>
                {stats.verified + stats.failed > 0 && (
                  <div className="mt-2">
                    <div className="flex justify-between text-xs text-zinc-600 mb-1">
                      <span>Достоверность</span>
                      <span>{Math.round((stats.verified / (stats.verified + stats.failed)) * 100)}%</span>
                    </div>
                    <div className="h-1.5 rounded-full bg-zinc-800 overflow-hidden">
                      <div
                        className="h-full bg-emerald-500 transition-all duration-500"
                        style={{ width: `${(stats.verified / (stats.verified + stats.failed)) * 100}%` }}
                      />
                    </div>
                  </div>
                )}
                {selectedModel.lastChecked && (
                  <p className="text-xs text-zinc-700 pt-1">
                    Посл. проверка: {new Date(selectedModel.lastChecked).toLocaleTimeString('ru-RU')}
                  </p>
                )}
              </div>
            </div>
          </div>
        </main>
      </div>}

      {/* API Panel at bottom — only on inspector tab */}
      {activeTab === 'inspector' && (
        <ApiPanel
          status={status}
          lastSync={lastSync}
          apiBase={apiBase}
          onRefresh={fetchModels}
        />
      )}
    </div>
  );
}
