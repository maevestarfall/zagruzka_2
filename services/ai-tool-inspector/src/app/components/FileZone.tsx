import { useState, useCallback, useRef } from 'react';
import JSZip from 'jszip';
import { Download, Trash2, FileArchive, File as FileIcon, Folder, Loader2 } from 'lucide-react';

export interface ZipEntry {
  path: string;
  isDir: boolean;
  size: number;
}

interface FileZoneProps {
  onFileRead: (filename: string, entries: ZipEntry[], blob: File) => void;
  onClear: () => void;
  hasFile: boolean;
}

// Store the object URL so we can trigger downloads from outside
let _downloadUrl: string | null = null;
let _downloadName: string | null = null;

export function triggerFileDownload() {
  if (!_downloadUrl || !_downloadName) return;
  const a = document.createElement('a');
  a.href = _downloadUrl;
  a.download = _downloadName;
  a.click();
}

export function FileZone({ onFileRead, onClear, hasFile }: FileZoneProps) {
  const [entries, setEntries] = useState<ZipEntry[]>([]);
  const [filename, setFilename] = useState<string>('');
  const [totalSize, setTotalSize] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const processFile = useCallback(async (f: File) => {
    setLoading(true);
    setError(null);
    try {
      const zip = await JSZip.loadAsync(f);
      const list: ZipEntry[] = [];
      let total = 0;
      zip.forEach((path, entry) => {
        const size = (entry as any)._data?.uncompressedSize ?? 0;
        list.push({ path, isDir: entry.dir, size });
        if (!entry.dir) total += size;
      });
      list.sort((a, b) => {
        if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
        return a.path.localeCompare(b.path);
      });

      if (_downloadUrl) URL.revokeObjectURL(_downloadUrl);
      _downloadUrl = URL.createObjectURL(f);
      _downloadName = f.name;

      setEntries(list);
      setFilename(f.name);
      setTotalSize(total);
      onFileRead(f.name, list, f);
    } catch {
      setError('Не удалось прочитать ZIP. Проверь формат файла.');
    } finally {
      setLoading(false);
    }
  }, [onFileRead]);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) processFile(f);
  }, [processFile]);

  const handleInput = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) processFile(f);
  }, [processFile]);

  const handleClear = useCallback(() => {
    if (_downloadUrl) { URL.revokeObjectURL(_downloadUrl); _downloadUrl = null; _downloadName = null; }
    setEntries([]);
    setFilename('');
    setTotalSize(0);
    setError(null);
    if (inputRef.current) inputRef.current.value = '';
    onClear();
  }, [onClear]);

  const fmtSize = (b: number) =>
    b >= 1024 * 1024 ? `${(b / 1024 / 1024).toFixed(1)} MB`
    : b >= 1024 ? `${(b / 1024).toFixed(1)} KB`
    : `${b} B`;

  const dirs  = entries.filter(e => e.isDir).length;
  const files = entries.filter(e => !e.isDir).length;

  // Empty state
  if (!hasFile || !entries.length) {
    return (
      <div
        className={`relative flex flex-col items-center justify-center gap-2 border-2 border-dashed rounded-xl transition-all cursor-pointer select-none mx-2 mb-2`}
        style={{
          height: 100,
          borderColor: dragging ? '#3b82f6' : '#334155',
          background: dragging ? 'rgba(59,130,246,0.07)' : 'rgba(15,23,42,0.5)',
        }}
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => inputRef.current?.click()}
      >
        <input ref={inputRef} type="file" accept=".zip,application/zip" className="hidden" onChange={handleInput} />
        {loading
          ? <Loader2 className="w-5 h-5 animate-spin text-blue-400" />
          : <FileArchive className="w-5 h-5" style={{ color: dragging ? '#3b82f6' : '#64748b' }} />
        }
        <span className="text-xs" style={{ color: dragging ? '#3b82f6' : '#64748b' }}>
          {loading ? 'Читаю ZIP...' : error ? error : 'Перетащи ZIP или нажми сюда'}
        </span>
      </div>
    );
  }

  // Filled state
  return (
    <div className="mx-2 mb-2 rounded-xl border overflow-hidden" style={{ borderColor: '#1e293b', background: 'rgba(15,23,42,0.7)' }}>
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2" style={{ borderBottom: '1px solid #1e293b' }}>
        <FileArchive className="w-4 h-4 shrink-0" style={{ color: '#3b82f6' }} />
        <span className="text-xs font-medium truncate flex-1" style={{ color: '#e2e8f0' }}>{filename}</span>
        <span className="text-xs shrink-0" style={{ color: '#64748b' }}>
          📁{dirs} 📄{files} · {fmtSize(totalSize)}
        </span>
        <button
          title="Скачать"
          onClick={triggerFileDownload}
          className="p-1 rounded hover:bg-blue-500/20 transition-colors"
          style={{ color: '#3b82f6' }}
        >
          <Download className="w-3.5 h-3.5" />
        </button>
        <button
          title="Удалить"
          onClick={handleClear}
          className="p-1 rounded hover:bg-red-500/20 transition-colors"
          style={{ color: '#ef4444' }}
        >
          <Trash2 className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* File tree */}
      <div className="overflow-y-auto" style={{ maxHeight: 120 }}>
        {entries.slice(0, 80).map((entry, i) => {
          const depth = (entry.path.match(/\//g) || []).length - (entry.isDir ? 1 : 0);
          const name = entry.path.split('/').filter(Boolean).at(-1) || entry.path;
          return (
            <div
              key={i}
              className="flex items-center gap-1.5 px-3 py-0.5 text-xs"
              style={{ paddingLeft: 12 + depth * 12, color: entry.isDir ? '#94a3b8' : '#cbd5e1' }}
            >
              {entry.isDir
                ? <Folder className="w-3 h-3 shrink-0" style={{ color: '#f59e0b' }} />
                : <FileIcon className="w-3 h-3 shrink-0" style={{ color: '#64748b' }} />
              }
              <span className="truncate">{name}</span>
              {!entry.isDir && entry.size > 0 && (
                <span className="ml-auto shrink-0 pl-2" style={{ color: '#475569' }}>{fmtSize(entry.size)}</span>
              )}
            </div>
          );
        })}
        {entries.length > 80 && (
          <div className="px-3 py-1 text-xs" style={{ color: '#475569' }}>
            ...и ещё {entries.length - 80} файлов
          </div>
        )}
      </div>
    </div>
  );
}
