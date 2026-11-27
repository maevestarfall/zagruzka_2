import { AIModel } from './types';
import { ShieldCheck, ShieldAlert, Activity, Circle } from 'lucide-react';

interface SidebarProps {
  models: AIModel[];
  selectedId: string;
  onSelect: (id: string) => void;
  globalMonitoring: boolean;
}

const providerColors: Record<string, string> = {
  Anthropic: '#d97706',
  OpenAI: '#10b981',
  Google: '#3b82f6',
  'Mistral AI': '#8b5cf6',
};

function verifiedCount(model: AIModel) {
  return model.tools.filter((t) => t.verificationStatus === 'verified').length;
}

function failedCount(model: AIModel) {
  return model.tools.filter((t) => t.verificationStatus === 'failed').length;
}

export function Sidebar({ models, selectedId, onSelect, globalMonitoring }: SidebarProps) {
  return (
    <aside className="w-64 shrink-0 bg-[#0f0f13] border-r border-white/10 flex flex-col h-full">
      <div className="px-4 py-4 border-b border-white/10">
        <div className="flex items-center gap-2">
          <Activity className="w-4 h-4 text-emerald-400" />
          <span className="text-xs text-zinc-400 uppercase tracking-widest">AI Агенты</span>
        </div>
      </div>
      <nav className="flex-1 overflow-y-auto py-2">
        {models.map((model) => {
          const isSelected = model.id === selectedId;
          const ok = verifiedCount(model);
          const fail = failedCount(model);
          const color = providerColors[model.provider] ?? '#888';

          return (
            <button
              key={model.id}
              onClick={() => onSelect(model.id)}
              className={`w-full text-left px-4 py-3 transition-colors group ${
                isSelected
                  ? 'bg-white/8 border-l-2'
                  : 'border-l-2 border-transparent hover:bg-white/5'
              }`}
              style={isSelected ? { borderLeftColor: color } : {}}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 min-w-0">
                  <Circle
                    className="w-2.5 h-2.5 shrink-0"
                    fill={color}
                    stroke="none"
                  />
                  <span
                    className="truncate text-sm"
                    style={{ color: isSelected ? '#fff' : '#a1a1aa' }}
                  >
                    {model.name}
                  </span>
                </div>
                {(globalMonitoring || model.monitoring) && (
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse shrink-0" />
                )}
              </div>
              <div className="ml-4 mt-1 flex items-center gap-3 text-xs text-zinc-600">
                <span className="text-zinc-500">{model.provider}</span>
                {ok > 0 && (
                  <span className="flex items-center gap-1 text-emerald-500">
                    <ShieldCheck className="w-3 h-3" />
                    {ok}
                  </span>
                )}
                {fail > 0 && (
                  <span className="flex items-center gap-1 text-red-500">
                    <ShieldAlert className="w-3 h-3" />
                    {fail}
                  </span>
                )}
              </div>
            </button>
          );
        })}
      </nav>
      <div className="px-4 py-3 border-t border-white/10 text-xs text-zinc-600">
        {models.length} агентов · {models.reduce((s, m) => s + m.tools.length, 0)} инструментов
      </div>
    </aside>
  );
}
