// Shared LiveKit helpers + React hooks, reused by both MAIN and NOTE screens.
// The transcription + data-channel logic here is lifted verbatim (in behavior)
// from the proven v2.0 App.tsx so the voice loop keeps working unchanged.

import { useCallback, useEffect, useRef, useState } from 'react';
import type { Room } from 'livekit-client';
import { RoomEvent } from 'livekit-client';

import type { TranscriptItem } from './types';

// LiveKit Agents publishes user STT + agent TTS as text streams on this topic.
const TRANSCRIPTION_TOPIC = 'lk.transcription';
const ATTR_SEGMENT_ID = 'lk.segment_id';
const ATTR_FINAL = 'lk.transcription_final';

// ----- UTF-8 helpers (fallback if Hermes lacks TextEncoder/TextDecoder) -----
export function encodeUtf8(s: string): Uint8Array {
  if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(s);
  const utf8 = unescape(encodeURIComponent(s));
  const arr = new Uint8Array(utf8.length);
  for (let i = 0; i < utf8.length; i++) arr[i] = utf8.charCodeAt(i);
  return arr;
}
export function decodeUtf8(bytes: Uint8Array): string {
  if (typeof TextDecoder !== 'undefined') return new TextDecoder().decode(bytes);
  let s = '';
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return decodeURIComponent(escape(s));
}

// Live transcription (user STT + agent TTS) as an ordered list of segments.
export function useTranscript(room: Room): TranscriptItem[] {
  const [transcript, setTranscript] = useState<TranscriptItem[]>([]);

  useEffect(() => {
    let cancelled = false;
    setTranscript([]);

    const upsert = (item: TranscriptItem) => {
      setTranscript((prev) => {
        const idx = prev.findIndex((t) => t.id === item.id);
        if (idx === -1) return [...prev, item];
        const next = prev.slice();
        next[idx] = item;
        return next;
      });
    };

    const handler = async (reader: any, info: { identity?: string }) => {
      const meta = reader?.info ?? {};
      const attrs: Record<string, string> = meta.attributes ?? {};
      const segmentId: string = attrs[ATTR_SEGMENT_ID] ?? meta.id;
      const isUser = info?.identity === room.localParticipant.identity;
      const role: 'user' | 'agent' = isUser ? 'user' : 'agent';
      const final = attrs[ATTR_FINAL] === 'true';

      let text = '';
      try {
        for await (const chunk of reader) {
          if (cancelled) return;
          text += chunk;
          upsert({ id: segmentId, role, text, final });
        }
        upsert({ id: segmentId, role, text, final });
      } catch (e) {
        console.warn('transcription stream error', e);
      }
    };

    try {
      room.registerTextStreamHandler(TRANSCRIPTION_TOPIC, handler);
    } catch {
      try {
        room.unregisterTextStreamHandler(TRANSCRIPTION_TOPIC);
        room.registerTextStreamHandler(TRANSCRIPTION_TOPIC, handler);
      } catch (e) {
        console.warn('could not register transcription handler', e);
      }
    }

    return () => {
      cancelled = true;
      try {
        room.unregisterTextStreamHandler(TRANSCRIPTION_TOPIC);
      } catch {}
    };
  }, [room]);

  return transcript;
}

// Subscribe to a single JSON data topic. onMessage is called with the parsed object.
export function useDataTopic(
  room: Room,
  topic: string,
  onMessage: (msg: any) => void,
): void {
  const cb = useRef(onMessage);
  cb.current = onMessage;

  useEffect(() => {
    const onData = (
      payload: Uint8Array,
      _participant?: unknown,
      _kind?: unknown,
      msgTopic?: string,
    ) => {
      if (msgTopic !== topic) return;
      try {
        cb.current(JSON.parse(decodeUtf8(payload)));
      } catch (e) {
        console.warn(`bad ${topic} payload`, e);
      }
    };
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room, topic]);
}

// Publish a JSON object to a data topic (reliable).
export function usePublish(room: Room) {
  return useCallback(
    (topic: string, obj: Record<string, unknown>) => {
      room.localParticipant.publishData(encodeUtf8(JSON.stringify(obj)), {
        reliable: true,
        topic,
      });
    },
    [room],
  );
}

// Track the room connection state ("connected" / "connecting" / ...).
export function useConnState(room: Room): string {
  const [state, setState] = useState<string>(room.state);
  useEffect(() => {
    const onState = () => setState(room.state);
    room.on(RoomEvent.ConnectionStateChanged, onState);
    onState();
    return () => {
      room.off(RoomEvent.ConnectionStateChanged, onState);
    };
  }, [room]);
  return state;
}

// Mic on/off control = the "start/stop conversation" mechanism. The room stays
// connected (live views + typed CRUD keep working); only the mic is toggled.
export function useMic(room: Room): [boolean, () => void] {
  const [micOn, setMicOn] = useState(true);
  useEffect(() => {
    room.localParticipant.setMicrophoneEnabled(micOn).catch((e) => {
      console.warn('setMicrophoneEnabled failed', e);
    });
  }, [room, micOn]);
  const toggle = useCallback(() => setMicOn((v) => !v), []);
  return [micOn, toggle];
}
