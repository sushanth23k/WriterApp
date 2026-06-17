// Token-server client. Asks the backend for a LiveKit token whose room metadata
// encodes the v3.0 state ("main" navigator room, or a per-doc "note" room).

import { UnauthorizedError } from './auth';
import { TOKEN_SERVER_URL } from './config';

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
