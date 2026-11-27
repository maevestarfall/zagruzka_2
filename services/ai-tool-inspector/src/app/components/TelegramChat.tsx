import { useEffect, useRef, useState } from 'react';
import { TgMessage, TgButton } from './workflowTypes';
import { Bot, Send, Paperclip, FileArchive, Download, X, Loader2, Eye, GitCompare, ChevronRight, ChevronDown, File as FileIcon } from 'lucide-react';
import JSZip from 'jszip';

export interface FileEntry {
  path: string;
  isDir: boolean;
  size: number;
}

interface TelegramChatProps {
  messages: TgMessage[];
  onAction: (action: string, data?: any) => void;
  onSend?: (text: string) => void;
  onFileAttach?: (filename: string, entries: FileEntry[], blob: File) => void;
  onCompareResult?: (result: string) => void;
  serverUrl?: string;
  inputPlaceholder?: string;
  inputActive?: boolean;
}

const variantStyle: Record<string, string> = {
  default: 'bg-[#2b5278] hover:bg-[#3a6899] text-white',
  primary: 'bg-[#5288c1] hover:bg-[#6299d2] text-white',
  danger:  'bg-[#8b2e2e] hover:bg-[#a03535] text-white',
  success: 'bg-[#2e6b3e] hover:bg-[#3a8a4f] text-white',
};

const TEXT_EXTS = new Set([
  'js','mjs','cjs','ts','tsx','jsx','json','md','txt','yaml','yml',
  'env','sh','bash','py','rb','go','css','html','xml','toml','ini',
  'cfg','conf','gitignore','dockerignore','dockerfile','lock','npmrc',
]);
function isTextFile(p: string) {
  const ext = p.split('.').pop()?.toLowerCase() || '';
  const base = p.split('/').pop()?.toLowerCase() || '';
  return TEXT_EXTS.has(ext) || TEXT_EXTS.has(base);
}

function fmtSize(b: number) {
  if (b >= 1024 * 1024) return `${(b / 1024 / 1024).toFixed(1)} MB`;
  if (b >= 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${b} B`;
}

interface QueuedFile { file: File; id: number; }

interface ProcessedFile {
  file: File;
  entries: FileEntry[];
  totalSize: number;
  serverPath?: string;
  status: 'processing' | 'done' | 'error';
  textContent: Record<string, string>; // path → content
}

let _qid = 0;

// ── Compare two ZIPs ──────────────────────────────────────────────────────────
function compareZips(a: ProcessedFile, b: ProcessedFile): string {
  const aFiles = new Set(a.entries.filter(e => !e.isDir).map(e => e.path));
  const bFiles = new Set(b.entries.filter(e => !e.isDir).map(e => e.path));

  const onlyInB: string[] = [];
  const onlyInA: string[] = [];
  const changed: string[] = [];

  bFiles.forEach(f => { if (!aFiles.has(f)) onlyInB.push(f); });
  aFiles.forEach(f => { if (!bFiles.has(f)) onlyInA.push(f); });
  bFiles.forEach(f => {
    if (aFiles.has(f)) {
      const ca = a.textContent[f];
      const cb = b.textContent[f];
      if (ca !== undefined && cb !== undefined && ca !== cb) changed.push(f);
    }
  });

  // For changed files, show a brief diff summary
  const changedDetails = changed.slice(0, 8).map(f => {
    const la = (a.textContent[f] || '').split('\n').length;
    const lb = (b.textContent[f] || '').split('\n').length;
    const diff = lb - la;
    return `  📝 ${f.split('/').pop()} — ${diff > 0 ? `+${diff}` : diff} строк`;
  });

  // Extract key new content from new files
  const newFileDetails = onlyInB.slice(0, 6).map(f => {
    const content = b.textContent[f];
    const preview = content ? content.slice(0, 120).replace(/\n/g, ' ').trim() : '(бинарный)';
    return `  📄 ${f}\n     ${preview}${(content?.length || 0) > 120 ? '...' : ''}`;
  });

  let out = `⚖️ Сравнение: ${a.file.name} → ${b.file.name}\n\n`;
  out += `📦 ${a.file.name}: ${aFiles.size} файлов\n`;
  out += `📦 ${b.file.name}: ${bFiles.size} файлов\n\n`;

  if (onlyInB.length > 0) {
    out += `🆕 Новые файлы в v6 (${onlyInB.length}):\n${newFileDetails.join('\n')}\n`;
    if (onlyInB.length > 6) out += `  ...и ещё ${onlyInB.length - 6}\n`;
    out += '\n';
  }

  if (changed.length > 0) {
    out += `✏️ Изменённые файлы (${changed.length}):\n${changedDetails.join('\n')}\n`;
    if (changed.length > 8) out += `  ...и ещё ${changed.length - 8}\n`;
    out += '\n';
  }

  if (onlyInA.length > 0) {
    out += `🗑 Удалено из v6 (${onlyInA.length}): ${onlyInA.slice(0, 5).map(f => f.split('/').pop()).join(', ')}\n\n`;
  }

  if (onlyInB.length === 0 && changed.length === 0) {
    out += '✅ Файлы идентичны — различий нет.';
  }

  return out;
}

// ── File viewer modal ─────────────────────────────────────────────────────────
function FileViewer({ file, onClose }: { file: ProcessedFile; onClose: () => void }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState('');

  const textFiles = Object.keys(file.textContent).filter(f =>
    !search || f.toLowerCase().includes(search.toLowerCase())
  ).sort();

  const toggleDir = (dir: string) => {
    setExpanded(prev => {
      const n = new Set(prev);
      n.has(dir) ? n.delete(dir) : n.add(dir);
      return n;
    });
  };

  // Build tree structure
  const tree: Record<string, string[]> = {};
  textFiles.forEach(f => {
    const parts = f.split('/');
    if (parts.length === 1) {
      if (!tree['']) tree[''] = [];
      tree[''].push(f);
    } else {
      const dir = parts.slice(0, -1).join('/');
      if (!tree[dir]) tree[dir] = [];
      tree[dir].push(f);
    }
  });

  const selectedContent = selected ? file.textContent[selected] : null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0,0,0,0.8)' }}>
      <div className="flex flex-col w-full max-w-5xl rounded-2xl overflow-hidden" style={{ height: '85vh', background: '#17212b', border: '1px solid #2a3f55' }}>
        {/* Header */}
        <div className="flex items-center gap-3 px-4 py-3 shrink-0" style={{ borderBottom: '1px solid #2a3f55' }}>
          <FileArchive className="w-5 h-5 text-[#5288c1]" />
          <span className="text-sm text-white font-medium flex-1 truncate">{file.file.name}</span>
          <span className="text-xs text-[#6c7883]">{Object.keys(file.textContent).length} текстовых файлов</span>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-white/10 transition-colors">
            <X className="w-4 h-4 text-[#6c7883]" />
          </button>
        </div>

        <div className="flex flex-1 min-h-0">
          {/* File tree */}
          <div className="w-64 shrink-0 flex flex-col" style={{ borderRight: '1px solid #1e2d3d' }}>
            <div className="p-2 shrink-0" style={{ borderBottom: '1px solid #1e2d3d' }}>
              <input
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="Поиск файлов..."
                className="w-full bg-[#0e1621] text-xs text-white px-2.5 py-1.5 rounded-lg outline-none placeholder-[#6c7883]"
                style={{ border: '1px solid #2a3f55' }}
              />
            </div>
            <div className="flex-1 overflow-y-auto py-1">
              {/* Root files */}
              {(tree[''] || []).map(f => (
                <button
                  key={f}
                  onClick={() => setSelected(f)}
                  className={`w-full flex items-center gap-2 px-3 py-1 text-xs text-left transition-colors ${selected === f ? 'bg-[#5288c1]/20 text-[#8faecf]' : 'text-[#94a3b8] hover:bg-white/5 hover:text-white'}`}
                >
                  <FileIcon className="w-3 h-3 shrink-0 text-[#64748b]" />
                  <span className="truncate">{f}</span>
                </button>
              ))}
              {/* Directories */}
              {Object.entries(tree).filter(([dir]) => dir !== '').map(([dir, files]) => (
                <div key={dir}>
                  <button
                    onClick={() => toggleDir(dir)}
                    className="w-full flex items-center gap-1.5 px-3 py-1 text-xs text-[#94a3b8] hover:bg-white/5 hover:text-white transition-colors"
                  >
                    {expanded.has(dir) ? <ChevronDown className="w-3 h-3 shrink-0" /> : <ChevronRight className="w-3 h-3 shrink-0" />}
                    <span className="truncate text-[#f59e0b]">{dir.split('/').pop()}/</span>
                  </button>
                  {expanded.has(dir) && files.map(f => (
                    <button
                      key={f}
                      onClick={() => setSelected(f)}
                      className={`w-full flex items-center gap-2 py-1 text-xs text-left transition-colors ${selected === f ? 'bg-[#5288c1]/20 text-[#8faecf]' : 'text-[#94a3b8] hover:bg-white/5 hover:text-white'}`}
                      style={{ paddingLeft: 24 + (dir.split('/').length - 1) * 8 }}
                    >
                      <FileIcon className="w-3 h-3 shrink-0 text-[#64748b]" />
                      <span className="truncate">{f.split('/').pop()}</span>
                    </button>
                  ))}
                </div>
              ))}
            </div>
          </div>

          {/* Content viewer */}
          <div className="flex-1 flex flex-col min-w-0">
            {selectedContent !== null ? (
              <>
                <div className="px-4 py-2 shrink-0 text-xs text-[#6c7883]" style={{ borderBottom: '1px solid #1e2d3d' }}>
                  {selected} · {selectedContent.split('\n').length} строк · {fmtSize(selectedContent.length)}
                </div>
                <pre className="flex-1 overflow-auto p-4 text-xs text-[#e2e8f0] font-mono leading-relaxed" style={{ background: '#0e1621' }}>
                  {selectedContent}
                </pre>
              </>
            ) : (
              <div className="flex-1 flex items-center justify-center text-[#6c7883] text-sm">
                ← Выбери файл для просмотра
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── TgInlineKeyboard ──────────────────────────────────────────────────────────
function TgInlineKeyboard({ rows, onAction }: { rows: TgButton[][], onAction: (a: string) => void }) {
  return (
    <div className="mt-1.5 space-y-1">
      {rows.map((row, ri) => (
        <div key={ri} className="flex gap-1">
          {row.map((btn) => (
            <button
              key={btn.action}
              onClick={() => onAction(btn.action)}
              className={`flex-1 px-3 py-1.5 rounded text-xs transition-all active:scale-95 ${variantStyle[btn.variant || 'default']}`}
            >
              {btn.label}
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}

function TypingIndicator() {
  return (
    <div className="flex items-end gap-2 mb-2">
      <div className="w-7 h-7 rounded-full bg-[#5288c1] flex items-center justify-center shrink-0">
        <Bot className="w-4 h-4 text-white" />
      </div>
      <div className="bg-[#182533] rounded-2xl rounded-bl-sm px-4 py-2.5">
        <div className="flex gap-1 items-center h-4">
          <span className="w-1.5 h-1.5 rounded-full bg-zinc-400 animate-bounce" style={{ animationDelay: '0ms' }} />
          <span className="w-1.5 h-1.5 rounded-full bg-zinc-400 animate-bounce" style={{ animationDelay: '150ms' }} />
          <span className="w-1.5 h-1.5 rounded-full bg-zinc-400 animate-bounce" style={{ animationDelay: '300ms' }} />
        </div>
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────
export function TelegramChat({
  messages, onAction, onSend, onFileAttach, onCompareResult, serverUrl, inputPlaceholder, inputActive,
}: TelegramChatProps) {
  const apiBase = (serverUrl || 'http://localhost:3001').replace(/\/$/, '');
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef  = useRef<HTMLInputElement>(null);
  const fileRef   = useRef<HTMLInputElement>(null);

  const [inputValue, setInputValue] = useState('');
  const [queue, setQueue]           = useState<QueuedFile[]>([]);
  const [processed, setProcessed]   = useState<ProcessedFile[]>([]);
  const [sending, setSending]       = useState(false);
  const [viewing, setViewing]       = useState<ProcessedFile | null>(null);
  const [comparing, setComparing]   = useState(false);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);
  useEffect(() => {
    if (inputActive) inputRef.current?.focus();
    else setInputValue('');
  }, [inputActive]);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files || []);
    if (!picked.length) return;
    if (fileRef.current) fileRef.current.value = '';
    setQueue(prev => [...prev, ...picked.map(f => ({ file: f, id: ++_qid }))]);
  };

  const removeFromQueue = (id: number) => setQueue(prev => prev.filter(q => q.id !== id));
  const removeProcessed = (idx: number) => setProcessed(prev => prev.filter((_, i) => i !== idx));

  const handleSendFiles = async () => {
    if (!queue.length || sending) return;
    setSending(true);
    const toProcess = [...queue];
    setQueue([]);

    const placeholders: ProcessedFile[] = toProcess.map(q => ({
      file: q.file, entries: [], totalSize: 0, status: 'processing', textContent: {},
    }));
    setProcessed(prev => [...prev, ...placeholders]);

    for (let i = 0; i < toProcess.length; i++) {
      const f = toProcess[i].file;
      try {
        const zip = await JSZip.loadAsync(f);
        const entries: FileEntry[] = [];
        let total = 0;

        zip.forEach((p, entry) => {
          const size = (entry as any)._data?.uncompressedSize ?? 0;
          entries.push({ path: p, isDir: entry.dir, size });
          if (!entry.dir) total += size;
        });
        entries.sort((a, b) => {
          if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
          return a.path.localeCompare(b.path);
        });

        // Extract text file contents
        const textContent: Record<string, string> = {};
        for (const entry of entries) {
          if (!entry.isDir && entry.size < 800_000 && isTextFile(entry.path)) {
            try {
              textContent[entry.path] = await zip.files[entry.path].async('string');
            } catch { /* skip unreadable */ }
          }
        }

        setProcessed(prev => prev.map(p =>
          p.file === f ? { ...p, entries, totalSize: total, status: 'done', textContent } : p
        ));

        // Also try server upload (optional, may fail)
        try {
          const small: Record<string, string> = {};
          Object.entries(textContent).forEach(([k, v]) => { if (v.length < 200_000) small[k] = v; });
          if (Object.keys(small).length > 0) {
            const resp = await fetch(`${apiBase}/api/extract-zip`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name: f.name, files: small }),
            });
            const data = await resp.json();
            if (data.path) {
              setProcessed(prev => prev.map(p => p.file === f ? { ...p, serverPath: data.path } : p));
            }
          }
        } catch { /* server offline is ok */ }

        onFileAttach?.(f.name, entries, f);
      } catch {
        setProcessed(prev => prev.map(p =>
          p.file === f ? { ...p, status: 'error', textContent: {} } : p
        ));
      }
    }
    setSending(false);
  };

  const handleCompare = () => {
    const done = processed.filter(p => p.status === 'done');
    if (done.length < 2) return;
    setComparing(true);
    setTimeout(() => {
      const result = compareZips(done[0], done[1]);
      onCompareResult?.(result);
      setComparing(false);
    }, 100);
  };

  const handleDownload = (f: File) => {
    const url = URL.createObjectURL(f);
    const a = document.createElement('a');
    a.href = url; a.download = f.name; a.click();
    URL.revokeObjectURL(url);
  };

  const handleTextSend = () => {
    const text = inputValue.trim();
    if (!text || !onSend) return;
    setInputValue('');
    onSend(text);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') handleTextSend();
  };

  const isTyping = messages[messages.length - 1]?.isTyping;
  const donePairs = processed.filter(p => p.status === 'done').length;

  return (
    <div className="flex flex-col h-full bg-[#0e1621] overflow-hidden">

      {/* File viewer modal */}
      {viewing && <FileViewer file={viewing} onClose={() => setViewing(null)} />}

      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3 bg-[#17212b] border-b border-white/5 shrink-0">
        <div className="w-9 h-9 rounded-full bg-[#5288c1] flex items-center justify-center">
          <Bot className="w-5 h-5 text-white" />
        </div>
        <div>
          <p className="text-sm text-white">AI Tool Inspector Bot</p>
          <p className="text-xs text-[#6c7883]">{isTyping ? 'печатает...' : 'онлайн'}</p>
        </div>
        <div className="ml-auto flex items-center gap-1">
          <span className="w-2 h-2 rounded-full bg-emerald-400" />
          <span className="text-xs text-emerald-400">Live</span>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-1">
        {messages.map((msg) => {
          if (msg.isTyping) return <TypingIndicator key={msg.id} />;
          if (msg.from === 'user') {
            return (
              <div key={msg.id} className="flex justify-end mb-2">
                <div className="max-w-[75%]">
                  <div className="bg-[#2b5278] rounded-2xl rounded-br-sm px-3.5 py-2">
                    <p className="text-sm text-white whitespace-pre-wrap">{msg.text}</p>
                  </div>
                  <p className="text-[10px] text-[#6c7883] text-right mt-0.5 pr-1">{msg.time}</p>
                </div>
              </div>
            );
          }
          return (
            <div key={msg.id} className="flex items-end gap-2 mb-2">
              <div className="w-7 h-7 rounded-full bg-[#5288c1] flex items-center justify-center shrink-0 mb-5">
                <Bot className="w-4 h-4 text-white" />
              </div>
              <div className="max-w-[80%]">
                <div className="bg-[#182533] rounded-2xl rounded-bl-sm px-3.5 py-2">
                  <p className="text-sm text-white whitespace-pre-wrap leading-relaxed">{msg.text}</p>
                  {msg.buttons && msg.buttons.length > 0 && (
                    <TgInlineKeyboard rows={msg.buttons} onAction={(a) => onAction(a)} />
                  )}
                </div>
                <p className="text-[10px] text-[#6c7883] mt-0.5 pl-1">{msg.time}</p>
              </div>
            </div>
          );
        })}
        <div ref={bottomRef} />
      </div>

      {/* Queue */}
      {queue.length > 0 && (
        <div className="shrink-0 mx-3 mb-1 rounded-xl border border-[#3a5f3a] bg-[#1a2b1a] overflow-hidden">
          <div className="flex items-center justify-between px-3 py-1.5 border-b border-[#2a4a2a]">
            <span className="text-[11px] text-emerald-400">
              📎 {queue.length} файл{queue.length > 1 ? 'а' : ''} в очереди
            </span>
            <button
              onClick={handleSendFiles}
              disabled={sending}
              className="flex items-center gap-1.5 px-3 py-1 rounded-lg text-[11px] font-medium transition-all active:scale-95 bg-[#5288c1] hover:bg-[#6299d2] text-white disabled:opacity-50"
            >
              {sending ? <Loader2 className="w-3 h-3 animate-spin" /> : <Send className="w-3 h-3" />}
              Прочитать
            </button>
          </div>
          <div className="max-h-28 overflow-y-auto">
            {queue.map((q) => (
              <div key={q.id} className="flex items-center gap-2 px-3 py-1.5">
                <FileArchive className="w-4 h-4 shrink-0 text-[#5288c1]" />
                <span className="flex-1 text-xs text-white truncate">{q.file.name}</span>
                <span className="text-[10px] text-[#6c7883] shrink-0">{fmtSize(q.file.size)}</span>
                <button onClick={() => removeFromQueue(q.id)} className="p-1 rounded hover:bg-red-500/20 transition-colors">
                  <X className="w-3 h-3 text-[#8b4444]" />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Processed files */}
      {processed.length > 0 && (
        <div className="shrink-0 mx-3 mb-1 space-y-1 max-h-56 overflow-y-auto">
          {/* Compare button when 2+ done */}
          {donePairs >= 2 && (
            <button
              onClick={handleCompare}
              disabled={comparing}
              className="w-full flex items-center justify-center gap-2 py-2 rounded-xl text-xs font-medium transition-all active:scale-95"
              style={{ background: 'rgba(82,136,193,0.15)', border: '1px solid #3a5f80', color: '#8faecf' }}
            >
              {comparing
                ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                : <GitCompare className="w-3.5 h-3.5" />
              }
              {comparing ? 'Сравниваю...' : `⚖️ Сравнить ${processed[0].file.name.split('.')[0]} ↔ ${processed[1].file.name.split('.')[0]}`}
            </button>
          )}

          {processed.map((item, idx) => {
            const dCnt = item.entries.filter(e => e.isDir).length;
            const fCnt = item.entries.filter(e => !e.isDir).length;
            const txtCnt = Object.keys(item.textContent).length;
            return (
              <div key={idx} className="rounded-xl overflow-hidden" style={{ border: '1px solid #2a3f55', background: '#17212b' }}>
                <div className="flex items-center gap-2.5 px-3 py-2">
                  <div className="w-8 h-8 rounded-lg bg-[#1e3a5f] flex items-center justify-center shrink-0">
                    {item.status === 'processing'
                      ? <Loader2 className="w-4 h-4 text-[#5288c1] animate-spin" />
                      : <FileArchive className="w-4 h-4 text-[#5288c1]" />
                    }
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-white truncate">{item.file.name}</p>
                    {item.status === 'processing'
                      ? <p className="text-[10px] text-yellow-400">⏳ читаю...</p>
                      : item.status === 'error'
                      ? <p className="text-[10px] text-red-400">❌ ошибка</p>
                      : <p className="text-[10px] text-[#6c7883]">
                          📁{dCnt} 📄{fCnt} · {fmtSize(item.totalSize)}
                          {txtCnt > 0 && <span className="text-emerald-400 ml-1">· 📖 {txtCnt} читаемых</span>}
                        </p>
                    }
                    {item.serverPath && (
                      <p className="text-[10px] text-emerald-400 truncate">✅ {item.serverPath}</p>
                    )}
                  </div>
                  {item.status === 'done' && txtCnt > 0 && (
                    <button
                      onClick={() => setViewing(item)}
                      title="Просмотр файлов"
                      className="p-1.5 rounded-lg hover:bg-[#2a3f55] transition-colors"
                    >
                      <Eye className="w-3.5 h-3.5 text-emerald-400" />
                    </button>
                  )}
                  <button onClick={() => handleDownload(item.file)} title="Скачать"
                    className="p-1.5 rounded-lg hover:bg-[#2a3f55] transition-colors">
                    <Download className="w-3.5 h-3.5 text-[#5288c1]" />
                  </button>
                  <button onClick={() => removeProcessed(idx)} title="Удалить"
                    className="p-1.5 rounded-lg hover:bg-[#3d1a1a] transition-colors">
                    <X className="w-3.5 h-3.5 text-[#8b4444]" />
                  </button>
                </div>

                {/* Mini file tree */}
                {item.entries.length > 0 && item.status === 'done' && (
                  <div className="border-t border-[#1e2d3d] max-h-16 overflow-y-auto">
                    {item.entries.slice(0, 20).map((entry, i) => {
                      const depth = (entry.path.match(/\//g) || []).length - (entry.isDir ? 1 : 0);
                      const name  = entry.path.split('/').filter(Boolean).at(-1) || entry.path;
                      const hasContent = !entry.isDir && !!item.textContent[entry.path];
                      return (
                        <button
                          key={i}
                          onClick={() => { if (hasContent) setViewing(item); }}
                          className={`w-full flex items-center gap-1.5 py-px text-[10px] text-left transition-colors ${hasContent ? 'hover:bg-[#5288c1]/10 cursor-pointer' : 'cursor-default'}`}
                          style={{ paddingLeft: 10 + depth * 8, paddingRight: 10, color: entry.isDir ? '#94a3b8' : hasContent ? '#86efac' : '#cbd5e1' }}
                        >
                          <span>{entry.isDir ? '📁' : hasContent ? '📖' : '📄'}</span>
                          <span className="truncate">{name}</span>
                          {!entry.isDir && entry.size > 0 && (
                            <span className="ml-auto pl-2 shrink-0 text-[#475569]">{fmtSize(entry.size)}</span>
                          )}
                        </button>
                      );
                    })}
                    {item.entries.length > 20 && (
                      <p className="px-3 py-px text-[10px] text-[#475569]">...ещё {item.entries.length - 20}</p>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Input bar */}
      <div className={`shrink-0 px-3 py-2 bg-[#17212b] border-t transition-colors ${inputActive ? 'border-[#5288c1]/60' : 'border-white/5'}`}>
        <div className={`flex items-center gap-2 rounded-xl px-3 py-1.5 transition-colors ${inputActive ? 'bg-[#1e2d3d] ring-1 ring-[#5288c1]/50' : 'bg-[#1a2533]'}`}>
          <button
            onClick={() => fileRef.current?.click()}
            title="Прикрепить файл"
            className="shrink-0 w-7 h-7 flex items-center justify-center rounded-full transition-colors hover:bg-white/10"
          >
            <Paperclip className="w-4 h-4 text-[#6c7883] hover:text-[#8faecf]" />
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".zip,application/zip,application/x-zip-compressed"
            multiple
            className="hidden"
            onChange={handleFileChange}
          />
          <input
            ref={inputRef}
            value={inputValue}
            onChange={e => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={inputPlaceholder || 'Сообщение...'}
            className="flex-1 bg-transparent text-sm text-white placeholder-[#6c7883] outline-none"
          />
          <button
            onClick={handleTextSend}
            disabled={!inputValue.trim()}
            className="shrink-0 w-7 h-7 rounded-full flex items-center justify-center transition-all disabled:opacity-30 bg-[#5288c1] hover:bg-[#6299d2] active:scale-90"
          >
            <Send className="w-3.5 h-3.5 text-white" />
          </button>
        </div>
      </div>
    </div>
  );
}
