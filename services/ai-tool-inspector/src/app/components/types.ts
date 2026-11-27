export type VerificationStatus = 'verified' | 'failed' | 'pending' | 'unchecked';

export type ToolCategory =
  | 'code'
  | 'web'
  | 'files'
  | 'data'
  | 'communication'
  | 'image'
  | 'audio'
  | 'memory'
  | 'system'
  | 'api';

export interface Capability {
  description: string;
  confirmed: boolean;
}

export interface Tool {
  id: string;
  name: string;
  description: string;
  category: ToolCategory;
  claimedCapabilities: Capability[];
  verificationStatus: VerificationStatus;
  verifiedAt: string | null;
  notes: string;
  reallyExists: boolean | null;
}

export interface VerificationLog {
  timestamp: string;
  toolId: string;
  toolName: string;
  result: 'verified' | 'failed';
  message: string;
}

export interface AIModel {
  id: string;
  name: string;
  provider: string;
  modelId: string;
  color: string;
  tools: Tool[];
  monitoring: boolean;
  lastChecked: string | null;
  verificationLog: VerificationLog[];
}
