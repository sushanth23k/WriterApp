// Token-server client. Two responsibilities:
//   1. fetchToken — mint a LiveKit token whose room metadata encodes the state
//      ("main" navigator room, or a per-doc "note" room) and the signed-in user.
//   2. the Notes REST API — read/write the user's notes DIRECTLY (no LiveKit
//      background channel). Every call carries the Bearer JWT, so the backend scopes
//      it to the signed-in user's own notes (writer_app schema).

import { UnauthorizedError } from './auth';
import { TOKEN_SERVER_URL } from './config';
import type { Doc, Entry, FullDoc } from './types';

export type TokenResult = {
  token: string;
  url: string;
  room: string;
  identity: string;
  mode: string | null;
  doc_id: string | null;
};

export async function fetchToken(
  mode: 'main' | 'note',
  jwt: string,
  docId?: string,
): Promise<TokenResult> {
  const body: Record<string, unknown> = { mode };
  if (mode === 'note') body.doc_id = docId;

  const res = await fetch(`${TOKEN_SERVER_URL}/token`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${jwt}`,
    },
    body: JSON.stringify(body),
  });
  // A rejected session must bubble up so the app can sign the user out.
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new Error(`token server ${res.status}`);
  return (await res.json()) as TokenResult;
}

// ----- Notes REST API ----------------------------------------------------------

// Shared authed fetch: attaches the Bearer token, maps 401 -> UnauthorizedError
// (so the app signs out), and returns the parsed JSON body (or undefined on 204).
async function authFetch<T>(
  path: string,
  jwt: string,
  init?: { method?: string; body?: unknown },
): Promise<T> {
  const res = await fetch(`${TOKEN_SERVER_URL}${path}`, {
    method: init?.method ?? 'GET',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${jwt}`,
    },
    body: init?.body === undefined ? undefined : JSON.stringify(init.body),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new Error(`notes api ${res.status}`);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const listDocs = (jwt: string) => authFetch<Doc[]>('/notes', jwt);

export const getDoc = (jwt: string, docId: string) =>
  authFetch<FullDoc>(`/notes/${docId}`, jwt);

export const createDoc = (jwt: string, title: string, description: string) =>
  authFetch<Doc>('/notes', jwt, { method: 'POST', body: { title, description } });

export const updateDoc = (
  jwt: string,
  docId: string,
  patch: { title?: string; description?: string },
) => authFetch<Doc>(`/notes/${docId}`, jwt, { method: 'PATCH', body: patch });

export const deleteDoc = (jwt: string, docId: string) =>
  authFetch<void>(`/notes/${docId}`, jwt, { method: 'DELETE' });

export const addEntry = (jwt: string, docId: string, text: string) =>
  authFetch<Entry>(`/notes/${docId}/entries`, jwt, { method: 'POST', body: { text } });

export const updateEntry = (
  jwt: string,
  docId: string,
  entryId: string,
  text: string,
) =>
  authFetch<Entry>(`/notes/${docId}/entries/${entryId}`, jwt, {
    method: 'PATCH',
    body: { text },
  });

export const deleteEntry = (jwt: string, docId: string, entryId: string) =>
  authFetch<void>(`/notes/${docId}/entries/${entryId}`, jwt, { method: 'DELETE' });
