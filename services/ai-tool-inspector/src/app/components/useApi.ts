import { useState, useEffect, useCallback, useRef } from 'react';
import { AIModel } from './types';

const API_BASE = 'http://localhost:3001';

export type ConnectionStatus = 'connecting' | 'connected' | 'error' | 'offline';

interface ApiState {
  models: AIModel[];
  status: ConnectionStatus;
  lastSync: string | null;
  error: string | null;
}

function adaptServerModel(raw: any): AIModel {
  return {
    id: raw.id,
    name: raw.name,
    provider: raw.provider,
    modelId: raw.modelId,
    color: colorForProvider(raw.provider),
    monitoring: raw.monitoring ?? false,
    lastChecked: raw.lastChecked ?? null,
    verificationLog: raw.verificationLog ?? [],
    tools: (raw.tools ?? []).map((t: any) => ({
      id: t.id,
      name: t.name,
      description: t.description ?? '',
      category: t.category ?? 'api',
      claimedCapabilities: t.claimedCapabilities ?? [],
      verificationStatus: t.verificationStatus ?? 'unchecked',
      verifiedAt: t.verifiedAt ?? null,
      notes: t.notes ?? '',
      reallyExists: t.reallyExists ?? null,
    })),
  };
}

function colorForProvider(provider: string): string {
  const map: Record<string, string> = {
    Anthropic: '#d97706',
    OpenAI: '#10b981',
    Google: '#3b82f6',
    'Mistral AI': '#8b5cf6',
  };
  return map[provider] ?? '#888';
}

export function useApi(pollInterval = 4000) {
  const [state, setState] = useState<ApiState>({
    models: [],
    status: 'connecting',
    lastSync: null,
    error: null,
  });

  const fetchModels = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/models`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      const detailResults = await Promise.all(
        data.models.map((m: any) => fetch(`${API_BASE}/api/models/${m.id}`).then((r) => r.json()))
      );

      setState((prev) => ({
        ...prev,
        models: detailResults.map(adaptServerModel),
        status: 'connected',
        lastSync: new Date().toISOString(),
        error: null,
      }));
    } catch (err: any) {
      setState((prev) => ({
        ...prev,
        status: prev.status === 'connected' ? 'error' : 'offline',
        error: err.message,
      }));
    }
  }, []);

  const verifyTool = useCallback(
    async (modelId: string, toolId: string, result: 'verified' | 'failed', message: string) => {
      const res = await fetch(`${API_BASE}/api/models/${modelId}/tools/${toolId}/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ result, message, source: 'inspector-ui' }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await fetchModels();
      return res.json();
    },
    [fetchModels]
  );

  const addTool = useCallback(
    async (modelId: string, tool: { name: string; description: string; category: string }) => {
      const res = await fetch(`${API_BASE}/api/models/${modelId}/tools`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(tool),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await fetchModels();
      return res.json();
    },
    [fetchModels]
  );

  // Initial fetch
  useEffect(() => {
    fetchModels();
  }, [fetchModels]);

  // Polling
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    timerRef.current = setInterval(fetchModels, pollInterval);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [fetchModels, pollInterval]);

  return { ...state, fetchModels, verifyTool, addTool, apiBase: API_BASE };
}
