import { Tool, ToolCategory, VerificationStatus } from './types';
import {
  ShieldCheck,
  ShieldAlert,
  ShieldQuestion,
  Clock,
  Code2,
  Globe,
  FolderOpen,
  Database,
  MessageSquare,
  Image,
  Mic,
  Brain,
  Settings,
  Plug,
  ChevronDown,
  ChevronUp,
  CheckCircle2,
  XCircle,
  HelpCircle,
} from 'lucide-react';
import { useState } from 'react';

const categoryIcons: Record<ToolCategory, React.ReactNode> = {
  code: <Code2 className="w-3.5 h-3.5" />,
  web: <Globe className="w-3.5 h-3.5" />,
  files: <FolderOpen className="w-3.5 h-3.5" />,
  data: <Database className="w-3.5 h-3.5" />,
  communication: <MessageSquare className="w-3.5 h-3.5" />,
  image: <Image className="w-3.5 h-3.5" />,
  audio: <Mic className="w-3.5 h-3.5" />,
  memory: <Brain className="w-3.5 h-3.5" />,
  system: <Settings className="w-3.5 h-3.5" />,
  api: <Plug className="w-3.5 h-3.5" />,
};

const categoryLabels: Record<ToolCategory, string> = {
  code: 'Код',
  web: 'Веб',
  files: 'Файлы',
  data: 'Данные',
  communication: 'Коммуникация',
  image: 'Изображения',
  audio: 'Аудио',
  memory: 'Память',
  system: 'Система',
  api: 'API',
};

const statusConfig: Record<
  VerificationStatus,
  { icon: React.ReactNode; label: string; color: string; bg: string }
> = {
  verified: {
    icon: <ShieldCheck className="w-4 h-4" />,
    label: 'Подтверждён',
    color: 'text-emerald-400',
    bg: 'bg-emerald-400/10 border-emerald-400/30',
  },
  failed: {
    icon: <ShieldAlert className="w-4 h-4" />,
    label: 'Не существует',
    color: 'text-red-400',
    bg: 'bg-red-400/10 border-red-400/30',
  },
  pending: {
    icon: <Clock className="w-4 h-4 animate-spin" />,
    label: 'Проверяется...',
    color: 'text-yellow-400',
    bg: 'bg-yellow-400/10 border-yellow-400/30',
  },
  unchecked: {
    icon: <ShieldQuestion className="w-4 h-4" />,
    label: 'Не проверен',
    color: 'text-zinc-500',
    bg: 'bg-zinc-800/50 border-zinc-700/50',
  },
};

interface ToolCardProps {
  tool: Tool;
  onVerify: (toolId: string) => void;
  isVerifying: boolean;
}

export function ToolCard({ tool, onVerify, isVerifying }: ToolCardProps) {
  const [expanded, setExpanded] = useState(false);
  const status = statusConfig[tool.verificationStatus];

  return (
    <div
      className={`rounded-lg border transition-all ${status.bg} ${
        expanded ? 'shadow-lg' : ''
      }`}
    >
      <div className="p-3">
        <div className="flex items-start justify-between gap-3">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <div className={`flex items-center gap-1.5 text-xs ${status.color}`}>
                {status.icon}
                <span>{status.label}</span>
              </div>
              <span className="flex items-center gap-1 text-xs text-zinc-600 bg-zinc-800/60 px-1.5 py-0.5 rounded">
                {categoryIcons[tool.category]}
                {categoryLabels[tool.category]}
              </span>
            </div>
            <h3 className="mt-1.5 text-sm text-white">{tool.name}</h3>
            <p className="mt-0.5 text-xs text-zinc-500 leading-relaxed">{tool.description}</p>
            {tool.notes && (
              <p className="mt-1 text-xs text-amber-500/80 italic">{tool.notes}</p>
            )}
          </div>
          <button
            onClick={() => onVerify(tool.id)}
            disabled={isVerifying || tool.verificationStatus === 'pending'}
            className={`shrink-0 px-3 py-1.5 rounded text-xs transition-colors ${
              tool.verificationStatus === 'pending'
                ? 'bg-yellow-400/10 text-yellow-400 cursor-not-allowed'
                : 'bg-zinc-700 hover:bg-zinc-600 text-zinc-200 cursor-pointer'
            }`}
          >
            {tool.verificationStatus === 'pending' ? 'Идёт...' : 'Проверить'}
          </button>
        </div>

        <button
          onClick={() => setExpanded((v) => !v)}
          className="mt-2 flex items-center gap-1 text-xs text-zinc-600 hover:text-zinc-400 transition-colors"
        >
          {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
          {tool.claimedCapabilities.length} заявленных возможностей
        </button>
      </div>

      {expanded && (
        <div className="px-3 pb-3 border-t border-white/5 mt-0 pt-3">
          <p className="text-xs text-zinc-500 mb-2 uppercase tracking-wider">Заявленные возможности</p>
          <ul className="space-y-1.5">
            {tool.claimedCapabilities.map((cap, i) => (
              <li key={i} className="flex items-start gap-2 text-xs">
                {cap.confirmed ? (
                  <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 mt-0.5 shrink-0" />
                ) : tool.verificationStatus === 'failed' ? (
                  <XCircle className="w-3.5 h-3.5 text-red-500 mt-0.5 shrink-0" />
                ) : (
                  <HelpCircle className="w-3.5 h-3.5 text-zinc-600 mt-0.5 shrink-0" />
                )}
                <span
                  className={
                    cap.confirmed
                      ? 'text-zinc-300'
                      : tool.verificationStatus === 'failed'
                      ? 'text-red-400 line-through'
                      : 'text-zinc-500'
                  }
                >
                  {cap.description}
                </span>
              </li>
            ))}
          </ul>
          {tool.verifiedAt && (
            <p className="mt-3 text-xs text-zinc-600">
              Проверено: {new Date(tool.verifiedAt).toLocaleString('ru-RU')}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
