// Inference-engine preference (persisted): which stack the agent runs.
//
//   'cloud'  — Deepgram STT + Groq llama-3.3-70b + Deepgram TTS (default; fast, smart)
//   'hybrid' — on-device MLX Whisper + Kokoro (audio stays private), but Groq
//              llama-3.3-70b for the LLM (dependable tool-calling)
//
// The choice is sent to the token server (see api.ts), which stamps it into the
// LiveKit room metadata + room name so the worker builds the matching pipeline.

import { useCallback, useEffect, useState } from 'react';
import * as SecureStore from 'expo-secure-store';

export type Engine = 'cloud' | 'hybrid';

const ENGINE_KEY = 'dropnote.engine';
const DEFAULT_ENGINE: Engine = 'cloud';

// Session hook: load the saved preference on mount, expose a persisted setter.
export function useEngine(): [Engine, (e: Engine) => void] {
  const [engine, setEngine] = useState<Engine>(DEFAULT_ENGINE);

  useEffect(() => {
    (async () => {
      const saved = await SecureStore.getItemAsync(ENGINE_KEY);
      if (saved === 'cloud' || saved === 'hybrid') setEngine(saved);
    })();
  }, []);

  const choose = useCallback((e: Engine) => {
    setEngine(e);
    SecureStore.setItemAsync(ENGINE_KEY, e).catch(() => {});
  }, []);

  return [engine, choose];
}
