import { useState } from 'react';
import { Workflow, WorkflowStep } from './workflowTypes';
import { Plus, Trash2, Clock, Play, ChevronDown, ChevronUp, ToggleLeft, ToggleRight, CheckCircle2, XCircle, Circle, Loader2 } from 'lucide-react';

const MODELS = [
  { id: 'claude-sonnet-4-6', name: 'Claude Sonnet 4.6', tools: ['bash','read','edit','write','grep','glob','web-search','agent','todo','skill'] },
  { id: 'gpt-4o', name: 'GPT-4o', tools: ['code-interpreter','web-browsing-oai','dalle','file-search','function-calling','vision-oai','memory-oai'] },
  { id: 'gemini-2-5-pro', name: 'Gemini 2.5 Pro', tools: ['google-search','code-execution','function-calling-gem','multimodal','thinking','context-cache'] },
  { id: 'mistral-large', name: 'Mistral Large', tools: ['function-calling-mis','vision-mis','web-search-mis'] },
];

const stepStatusIcon = (status: WorkflowStep['status']) => {
  if (status === 'completed') return <CheckCircle2 className="w-4 h-4 text-emerald-400" />;
  if (status === 'failed') return <XCircle className="w-4 h-4 text-red-400" />;
  if (status === 'running') return <Loader2 className="w-4 h-4 text-yellow-400 animate-spin" />;
  if (status === 'waiting_approval') return <Clock className="w-4 h-4 text-blue-400 animate-pulse" />;
  return <Circle className="w-4 h-4 text-zinc-600" />;
};

interface WorkflowPanelProps {
  workflows: Workflow[];
  activeWorkflow: Workflow | null;
  onCreateWorkflow: (wf: Omit<Workflow, 'id' | 'createdAt' | 'status' | 'currentStepIndex' | 'startedAt'>) => void;
  onRunWorkflow: (id: string) => void;
  onSelectWorkflow: (id: string) => void;
  onDeleteWorkflow: (id: string) => void;
  onToggleAutoContinue: (id: string) => void;
  stepTimers: Record<string, number>;
}

function StepRow({ step, idx, timer }: { step: WorkflowStep; idx: number; timer?: number }) {
  const [open, setOpen] = useState(false);
  const isActive = step.status === 'running' || step.status === 'waiting_approval';

  return (
    <div className={`rounded border transition-colors ${isActive ? 'border-yellow-500/40 bg-yellow-500/5' : 'border-white/10 bg-white/3'}`}>
      <button className="w-full flex items-center gap-2 p-2.5 text-left" onClick={() => setOpen(v => !v)}>
        {stepStatusIcon(step.status)}
        <span className="flex-1 text-xs text-zinc-300 truncate">{idx + 1}. {step.name}</span>
        {isActive && timer !== undefined && (
          <span className={`text-xs px-2 py-0.5 rounded ${timer <= 60 ? 'bg-red-500/20 text-red-400' : 'bg-yellow-500/20 text-yellow-400'}`}>
            {Math.floor(timer / 60)}:{String(timer % 60).padStart(2, '0')}
          </span>
        )}
        <span className="text-zinc-600">{open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}</span>
      </button>
      {open && (
        <div className="px-3 pb-2.5 border-t border-white/5 pt-2 text-xs space-y-1">
          <p className="text-zinc-500">{step.description || 'Нет описания'}</p>
          <p className="text-zinc-600">Модель: <span className="text-zinc-400">{step.modelId}</span></p>
          <p className="text-zinc-600">Лимит: <span className="text-zinc-400">{step.timeLimitMinutes} мин</span></p>
          {step.toolIds.length > 0 && (
            <p className="text-zinc-600">Инструменты: <span className="text-zinc-400">{step.toolIds.join(', ')}</span></p>
          )}
          {step.result && (
            <p className={step.result === 'passed' ? 'text-emerald-400' : 'text-red-400'}>
              Результат: {step.result === 'passed' ? '✅ Пройдено' : '❌ Провал'}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Create Workflow Form ─────────────────────────────────────────────────────

interface NewStep {
  name: string;
  description: string;
  timeLimitMinutes: number;
  modelId: string;
  toolIds: string[];
}

function blankStep(): NewStep {
  return { name: '', description: '', timeLimitMinutes: 5, modelId: 'claude-sonnet-4-6', toolIds: [] };
}

function CreateForm({ onSubmit, onCancel }: { onSubmit: (data: any) => void; onCancel: () => void }) {
  const [name, setName] = useState('');
  const [desc, setDesc] = useState('');
  const [autoContinue, setAutoContinue] = useState(true);
  const [steps, setSteps] = useState<NewStep[]>([blankStep()]);

  const updateStep = (i: number, field: keyof NewStep, val: any) => {
    setSteps(prev => prev.map((s, idx) => idx === i ? { ...s, [field]: val } : s));
  };

  const toggleTool = (stepIdx: number, toolId: string) => {
    setSteps(prev => prev.map((s, i) => {
      if (i !== stepIdx) return s;
      const tools = s.toolIds.includes(toolId) ? s.toolIds.filter(t => t !== toolId) : [...s.toolIds, toolId];
      return { ...s, toolIds: tools };
    }));
  };

  const submit = () => {
    if (!name.trim() || steps.some(s => !s.name.trim())) return;
    onSubmit({ name, description: desc, autoContinue, steps: steps.map((s, i) => ({
      id: `step_${Date.now()}_${i}`, ...s,
      status: 'pending', startedAt: null, completedAt: null, result: null, verifiedTools: [],
    })) });
  };

  const input = "w-full bg-[#17212b] border border-white/10 rounded px-2.5 py-1.5 text-xs text-white placeholder-zinc-600 outline-none focus:border-[#5288c1]";

  return (
    <div className="space-y-3 text-xs">
      <input className={input} placeholder="Название алгоритма*" value={name} onChange={e => setName(e.target.value)} />
      <input className={input} placeholder="Описание (опционально)" value={desc} onChange={e => setDesc(e.target.value)} />

      {/* Auto-continue toggle */}
      <button
        onClick={() => setAutoContinue(v => !v)}
        className={`w-full flex items-center justify-between px-3 py-2 rounded border transition-colors ${autoContinue ? 'border-emerald-500/40 bg-emerald-500/10' : 'border-white/10 bg-white/5'}`}
      >
        <div>
          <p className={`text-xs ${autoContinue ? 'text-emerald-400' : 'text-zinc-400'}`}>
            {autoContinue ? '✅ Авто-продолжение: ВКЛ' : '⏸ Авто-продолжение: ВЫКЛ'}
          </p>
          <p className="text-[10px] text-zinc-600 mt-0.5">
            {autoContinue ? 'После выполнения шага — автоматически переходит к следующему' : 'После каждого шага ждёт твоего подтверждения'}
          </p>
        </div>
        {autoContinue ? <ToggleRight className="w-5 h-5 text-emerald-400 shrink-0" /> : <ToggleLeft className="w-5 h-5 text-zinc-600 shrink-0" />}
      </button>

      {/* Steps */}
      <p className="text-zinc-500 uppercase tracking-wider text-[10px]">Шаги алгоритма</p>
      {steps.map((step, i) => {
        const modelTools = MODELS.find(m => m.id === step.modelId)?.tools || [];
        return (
          <div key={i} className="rounded border border-white/10 bg-white/3 p-2.5 space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-zinc-500">Шаг {i + 1}</span>
              {steps.length > 1 && (
                <button onClick={() => setSteps(prev => prev.filter((_, idx) => idx !== i))} className="text-zinc-600 hover:text-red-400">
                  <Trash2 className="w-3 h-3" />
                </button>
              )}
            </div>
            <input className={input} placeholder="Название шага*" value={step.name} onChange={e => updateStep(i, 'name', e.target.value)} />
            <input className={input} placeholder="Описание" value={step.description} onChange={e => updateStep(i, 'description', e.target.value)} />
            <div className="flex gap-2">
              <select
                className={`${input} flex-1`}
                value={step.modelId}
                onChange={e => updateStep(i, 'modelId', e.target.value)}
              >
                {MODELS.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
              </select>
              <div className="flex items-center gap-1.5 bg-[#17212b] border border-white/10 rounded px-2.5 py-1.5 shrink-0">
                <Clock className="w-3 h-3 text-zinc-500" />
                <input
                  type="number" min={1} max={120}
                  value={step.timeLimitMinutes}
                  onChange={e => updateStep(i, 'timeLimitMinutes', Number(e.target.value))}
                  className="w-10 bg-transparent text-white text-xs outline-none"
                />
                <span className="text-zinc-600">мин</span>
              </div>
            </div>
            {/* Tool checkboxes */}
            <div className="flex flex-wrap gap-1">
              {modelTools.map(t => (
                <button
                  key={t}
                  onClick={() => toggleTool(i, t)}
                  className={`px-2 py-0.5 rounded text-[10px] transition-colors ${step.toolIds.includes(t) ? 'bg-[#5288c1]/30 text-[#5288c1] border border-[#5288c1]/50' : 'bg-white/5 text-zinc-600 border border-white/10 hover:border-zinc-600'}`}
                >
                  {t}
                </button>
              ))}
            </div>
          </div>
        );
      })}

      <button
        onClick={() => setSteps(prev => [...prev, blankStep()])}
        className="w-full border border-dashed border-white/20 rounded py-2 text-zinc-600 hover:text-zinc-400 hover:border-zinc-600 transition-colors flex items-center justify-center gap-1"
      >
        <Plus className="w-3 h-3" />добавить шаг
      </button>

      <div className="flex gap-2 pt-1">
        <button onClick={submit} className="flex-1 bg-[#5288c1] hover:bg-[#6299d2] text-white py-2 rounded transition-colors">
          Создать алгоритм
        </button>
        <button onClick={onCancel} className="px-4 bg-white/5 hover:bg-white/10 text-zinc-400 py-2 rounded transition-colors">
          Отмена
        </button>
      </div>
    </div>
  );
}

// ─── Main Panel ───────────────────────────────────────────────────────────────

export function WorkflowPanel({
  workflows, activeWorkflow, onCreateWorkflow, onRunWorkflow,
  onSelectWorkflow, onDeleteWorkflow, onToggleAutoContinue, stepTimers,
}: WorkflowPanelProps) {
  const [creating, setCreating] = useState(false);

  const statusBadge = (s: Workflow['status']) => {
    const map = { idle: 'text-zinc-500', running: 'text-yellow-400', completed: 'text-emerald-400', paused: 'text-blue-400', failed: 'text-red-400' };
    const labels = { idle: 'Ожидание', running: '▶ Запущен', completed: '✅ Завершён', paused: '⏸ На паузе', failed: '❌ Ошибка' };
    return <span className={`text-[10px] ${map[s]}`}>{labels[s]}</span>;
  };

  return (
    <div className="flex flex-col h-full bg-[#17212b] border-l border-white/10">
      <div className="px-4 py-3 border-b border-white/10 flex items-center justify-between shrink-0">
        <p className="text-xs text-zinc-300 uppercase tracking-wider">Алгоритмы</p>
        {!creating && (
          <button onClick={() => setCreating(true)} className="flex items-center gap-1 text-xs text-[#5288c1] hover:text-[#6299d2]">
            <Plus className="w-3.5 h-3.5" />Новый
          </button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {creating && (
          <div className="bg-[#0e1621] rounded-lg p-3 border border-[#5288c1]/30">
            <p className="text-xs text-[#5288c1] mb-2">Новый алгоритм</p>
            <CreateForm
              onSubmit={(data) => { onCreateWorkflow(data); setCreating(false); }}
              onCancel={() => setCreating(false)}
            />
          </div>
        )}

        {workflows.length === 0 && !creating && (
          <div className="text-center py-8 text-zinc-700 text-xs">
            <p>Нет алгоритмов</p>
            <p>Нажми «Новый» чтобы создать</p>
          </div>
        )}

        {workflows.map(wf => {
          const isActive = activeWorkflow?.id === wf.id;
          const currentStep = wf.steps[wf.currentStepIndex];
          const timer = currentStep ? stepTimers[currentStep.id] : undefined;

          return (
            <div
              key={wf.id}
              onClick={() => onSelectWorkflow(wf.id)}
              className={`rounded-lg border p-3 cursor-pointer transition-colors ${isActive ? 'border-[#5288c1]/50 bg-[#5288c1]/10' : 'border-white/10 bg-white/3 hover:border-white/20'}`}
            >
              <div className="flex items-start justify-between gap-2 mb-2">
                <div className="min-w-0">
                  <p className="text-xs text-white truncate">{wf.name}</p>
                  {statusBadge(wf.status)}
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  {/* Auto-continue toggle */}
                  <button
                    onClick={e => { e.stopPropagation(); onToggleAutoContinue(wf.id); }}
                    title={wf.autoContinue ? 'Авто-продолжение вкл' : 'Авто-продолжение выкл'}
                    className={`text-xs px-1.5 py-0.5 rounded border transition-colors ${wf.autoContinue ? 'border-emerald-500/40 text-emerald-400 bg-emerald-500/10' : 'border-white/15 text-zinc-600'}`}
                  >
                    {wf.autoContinue ? 'авто' : 'ручной'}
                  </button>
                  {wf.status === 'idle' || wf.status === 'paused' ? (
                    <button
                      onClick={e => { e.stopPropagation(); onRunWorkflow(wf.id); }}
                      className="p-1 bg-[#5288c1]/20 hover:bg-[#5288c1]/40 text-[#5288c1] rounded transition-colors"
                    >
                      <Play className="w-3 h-3" />
                    </button>
                  ) : null}
                  <button
                    onClick={e => { e.stopPropagation(); onDeleteWorkflow(wf.id); }}
                    className="p-1 text-zinc-700 hover:text-red-400 rounded transition-colors"
                  >
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
              </div>

              {/* Steps */}
              <div className="space-y-1">
                {wf.steps.map((step, i) => (
                  <StepRow key={step.id} step={step} idx={i} timer={isActive && step.status === 'running' ? timer : undefined} />
                ))}
              </div>

              {/* Settings summary */}
              <div className="mt-2 flex items-center gap-3 text-[10px] text-zinc-600">
                <span>{wf.steps.length} шагов</span>
                <span>{wf.steps.reduce((s, st) => s + st.timeLimitMinutes, 0)} мин общий лимит</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
