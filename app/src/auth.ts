// Auth client + session persistence for DropNote.
//
// The app signs in against the token server (POST /login) to get a JWT, stores it
// in the device keychain (expo-secure-store) so the session survives app restarts,
// and sends it as a Bearer token when minting LiveKit tokens (see api.ts).

import { useCallback, useEffect, useState } from 'react';
import * as SecureStore from 'expo-secure-store';

import { TOKEN_SERVER_URL } from './config';

const TOKEN_KEY = 'dropnote.jwt';

// Thrown when a protected request is rejected — the app should sign out on this.
export class UnauthorizedError extends Error {
  constructor(message = 'Session expired') {
    super(message);
    this.name = 'UnauthorizedError';
  }
}

export async function login(email: string, password: string): Promise<string> {
  const res = await fetch(`${TOKEN_SERVER_URL}/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: email.trim().toLowerCase(), password }),
  });
  if (res.status === 401) throw new Error('Invalid email or password.');
  if (!res.ok) throw new Error(`Sign-in failed (${res.status}).`);
  const data = (await res.json()) as { access_token: string };
  return data.access_token;
}

export async function saveToken(token: string): Promise<void> {
  await SecureStore.setItemAsync(TOKEN_KEY, token);
}

export async function loadToken(): Promise<string | null> {
  return SecureStore.getItemAsync(TOKEN_KEY);
}

export async function clearToken(): Promise<void> {
  await SecureStore.deleteItemAsync(TOKEN_KEY);
}

// Session hook: loads any persisted token on mount, exposes sign-in / sign-out.
export function useAuth() {
  const [token, setToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        setToken(await loadToken());
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const signIn = useCallback(async (email: string, password: string) => {
    const jwt = await login(email, password);
    await saveToken(jwt);
    setToken(jwt);
  }, []);

  const signOut = useCallback(async () => {
    await clearToken();
    setToken(null);
  }, []);

  return { token, loading, signIn, signOut };
}
