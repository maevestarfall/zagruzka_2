export interface WorkflowStep {
  id: string;
  name: string;
  description: string;
  timeLimitMinutes: number;
  modelId: string;
  toolIds: string[];
  status: 'pending' | 'running' | 'completed' | 'failed' | 'waiting_approval';
  startedAt: string | null;
  completedAt: string | null;
  result: 'passed' | 'failed' | null;
  verifiedTools: string[];
}

export interface Workflow {
  id: string;
  name: string;
  description: string;
  steps: WorkflowStep[];
  autoContinue: boolean;
  currentStepIndex: number;
  status: 'idle' | 'running' | 'completed' | 'paused' | 'failed';
  createdAt: string;
  startedAt: string | null;
}

export interface TgButton {
  label: string;
  action: string;
  variant?: 'default' | 'primary' | 'danger' | 'success';
}

export interface TgMessage {
  id: string;
  from: 'bot' | 'user';
  text: string;
  time: string;
  buttons?: TgButton[][];
  isTyping?: boolean;
}

export interface AiTask {
  id: string;
  text: string;
  source: 'text' | 'import' | 'algorithm';
  status: 'pending' | 'running' | 'done';
  createdAt: string;
}

export interface AiCapability {
  id: string;
  label: string;
  description: string;
  enabled: boolean;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface AiSlot {
  id: number;
  connected: boolean;
  name?: string;
  provider?: string;
  domain?: string;
  serverUrl?: string;
  token?: string;
  apiKey?: string;
  modelApiId?: string;
  tasks: AiTask[];
  capabilities: AiCapability[];
  chatHistory: ChatMessage[];
}
