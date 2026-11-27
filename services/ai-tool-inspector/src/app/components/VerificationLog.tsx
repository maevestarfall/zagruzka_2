import { VerificationLog as LogEntry } from './types';
import { ShieldCheck, ShieldAlert, ScrollText } from 'lucide-react';

interface VerificationLogProps {
  logs: LogEntry[];
}

export function VerificationLogPanel({ logs }: VerificationLogProps) {
  if (logs.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-32 text-zinc-700">
        <ScrollText className="w-6 h-6 mb-2" />
        <p className="text-xs">Лог пуст — запустите проверку</p>
      </div>
    );
  }

  return (
    <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
      {[...logs].reverse().map((entry, i) => (
        <div
          key={i}
          className={`flex items-start gap-2 p-2 rounded text-xs border ${
            entry.result === 'verified'
              ? 'bg-emerald-400/5 border-emerald-400/20'
              : 'bg-red-400/5 border-red-400/20'
          }`}
        >
          {entry.result === 'verified' ? (
            <ShieldCheck className="w-3.5 h-3.5 text-emerald-400 mt-0.5 shrink-0" />
          ) : (
            <ShieldAlert className="w-3.5 h-3.5 text-red-400 mt-0.5 shrink-0" />
          )}
          <div className="min-w-0">
            <span className="text-zinc-400">{entry.toolName}</span>
            <span className="mx-1 text-zinc-700">·</span>
            <span className={entry.result === 'verified' ? 'text-emerald-400' : 'text-red-400'}>
              {entry.message}
            </span>
            <div className="text-zinc-700 mt-0.5">
              {new Date(entry.timestamp).toLocaleTimeString('ru-RU')}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
