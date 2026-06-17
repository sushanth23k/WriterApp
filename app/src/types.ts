// Shared types mirroring the backend data contract (store.py + agent.py topics).

// A note doc as seen in the MAIN list (no entries — the navigator never loads them).
export type Doc = {
  id: string;
  title: string;
  description: string;
  created_at: number;
  updated_at: number;
};

// A single jotted line inside a doc.
export type Entry = {
  id: string;
  doc_id: string;
  text: string;
  created_at: number;
};

// A doc with its entries loaded (NOTE state).
export type FullDoc = Doc & { entries: Entry[] };

// Live transcription line (user STT or agent TTS).
export type TranscriptItem = {
  id: string;
  role: 'user' | 'agent';
  text: string;
  final: boolean;
};

// App state machine.
export type Screen =
  | { mode: 'main' }
  | { mode: 'note'; docId: string; title: string };

// ----- data-channel topics (must match agent.py) -----
export const TOPICS = {
  // MAIN
  docs: 'docs', // agent -> app: full docs list
  docsEdit: 'docs-edit', // app -> agent: typed doc CRUD
  navigate: 'navigate', // agent -> app: transition into a doc
  // NOTE
  doc: 'doc', // agent -> app: the single current doc (+entries)
  docEdit: 'doc-edit', // app -> agent: typed entry/meta CRUD
  // voice state-movement control (agent -> app): "go_back" / "stop"
  control: 'control',
} as const;
