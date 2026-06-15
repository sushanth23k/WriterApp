import { StatusBar } from 'expo-status-bar';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActivityIndicator,
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import {
  AudioSession,
  LiveKitRoom,
  useRoomContext,
} from '@livekit/react-native';
import { RoomEvent } from 'livekit-client';

import { TOKEN_SERVER_URL } from './src/config';

// ----- types -----
type Note = {
  id: string;
  text: string;
  tags: string;
  created_at: number;
  updated_at: number;
};

type TranscriptItem = {
  id: string; // the segment id (stable across interim updates)
  role: 'user' | 'agent';
  text: string;
  final: boolean;
};

// LiveKit Agents publishes both the user's STT and the agent's TTS text as text
// streams on this topic, with per-segment attributes. (The legacy
// RoomEvent.TranscriptionReceived event does NOT fire for text-stream transcripts.)
const TRANSCRIPTION_TOPIC = 'lk.transcription';
const ATTR_SEGMENT_ID = 'lk.segment_id';
const ATTR_FINAL = 'lk.transcription_final';

// ----- UTF-8 helpers (fallback if Hermes lacks TextEncoder/TextDecoder) -----
function encodeUtf8(s: string): Uint8Array {
  if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(s);
  const utf8 = unescape(encodeURIComponent(s));
  const arr = new Uint8Array(utf8.length);
  for (let i = 0; i < utf8.length; i++) arr[i] = utf8.charCodeAt(i);
  return arr;
}
function decodeUtf8(bytes: Uint8Array): string {
  if (typeof TextDecoder !== 'undefined') return new TextDecoder().decode(bytes);
  let s = '';
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return decodeURIComponent(escape(s));
}

// =====================================================================
// Inner view: rendered inside <LiveKitRoom>, has access to the Room.
// =====================================================================
function ConversationView({ onEnd }: { onEnd: () => void }) {
  const room = useRoomContext();
  const [notes, setNotes] = useState<Note[]>([]);
  const [draft, setDraft] = useState('');
  const [connState, setConnState] = useState<string>(room.state);
  const [transcript, setTranscript] = useState<TranscriptItem[]>([]);
  const transcriptRef = useRef<FlatList<TranscriptItem>>(null);

  // Subscribe to live transcriptions (user STT + agent TTS) and render them.
  useEffect(() => {
    let cancelled = false;

    const upsert = (item: TranscriptItem) => {
      setTranscript((prev) => {
        const idx = prev.findIndex((t) => t.id === item.id);
        if (idx === -1) return [...prev, item];
        const next = prev.slice();
        next[idx] = item;
        return next;
      });
    };

    const handler = async (
      reader: any,
      participantInfo: { identity?: string },
    ) => {
      const info = reader?.info ?? {};
      const attrs: Record<string, string> = info.attributes ?? {};
      const segmentId: string = attrs[ATTR_SEGMENT_ID] ?? info.id;
      const isUser = participantInfo?.identity === room.localParticipant.identity;
      const role: 'user' | 'agent' = isUser ? 'user' : 'agent';
      const final = attrs[ATTR_FINAL] === 'true';

      let text = '';
      try {
        for await (const chunk of reader) {
          if (cancelled) return;
          text += chunk;
          upsert({ id: segmentId, role, text, final });
        }
        // Stream complete — keep whatever final flag the attributes carried.
        upsert({ id: segmentId, role, text, final });
      } catch (e) {
        console.warn('transcription stream error', e);
      }
    };

    // Only one handler may be registered per topic; replace any stale one
    // (e.g. after a fast remount) before registering.
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

  // Subscribe to "notes" broadcasts from the agent (live updates).
  useEffect(() => {
    const onData = (
      payload: Uint8Array,
      _participant?: unknown,
      _kind?: unknown,
      topic?: string,
    ) => {
      if (topic !== 'notes') return;
      try {
        const msg = JSON.parse(decodeUtf8(payload));
        if (msg.type === 'notes' && Array.isArray(msg.notes)) setNotes(msg.notes);
      } catch (e) {
        console.warn('bad notes payload', e);
      }
    };
    const onState = () => setConnState(room.state);

    room.on(RoomEvent.DataReceived, onData);
    room.on(RoomEvent.ConnectionStateChanged, onState);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
      room.off(RoomEvent.ConnectionStateChanged, onState);
    };
  }, [room]);

  // Publish a "notes-edit" message to the agent (typed CRUD).
  const publishEdit = useCallback(
    (obj: Record<string, unknown>) => {
      const bytes = encodeUtf8(JSON.stringify(obj));
      room.localParticipant.publishData(bytes, { reliable: true, topic: 'notes-edit' });
    },
    [room],
  );

  const addNote = useCallback(() => {
    const t = draft.trim();
    if (!t) return;
    publishEdit({ action: 'create', text: t });
    setDraft('');
  }, [draft, publishEdit]);

  const connected = connState === 'connected';

  return (
    <View style={styles.flex}>
      <View style={styles.statusRow}>
        <View style={[styles.pill, connected ? styles.pillOk : styles.pillWarn]}>
          <Text style={styles.pillText}>
            {connected ? '🎙️ Listening' : `Connecting… (${connState})`}
          </Text>
        </View>
        <Pressable style={[styles.btn, styles.btnEnd]} onPress={onEnd}>
          <Text style={styles.btnText}>End</Text>
        </Pressable>
      </View>

      <Text style={styles.sectionTitle}>Conversation</Text>
      <FlatList
        ref={transcriptRef}
        style={styles.flex}
        data={transcript}
        keyExtractor={(t) => t.id}
        onContentSizeChange={() => transcriptRef.current?.scrollToEnd({ animated: true })}
        ListEmptyComponent={
          <Text style={styles.empty}>
            {connected ? 'Listening… start talking.' : 'Connecting…'}
          </Text>
        }
        renderItem={({ item }) => (
          <View
            style={[
              styles.bubble,
              item.role === 'user' ? styles.bubbleUser : styles.bubbleAgent,
            ]}
          >
            <Text style={styles.bubbleRole}>
              {item.role === 'user' ? 'You' : 'Assistant'}
            </Text>
            <Text style={[styles.bubbleText, !item.final && styles.bubbleInterim]}>
              {item.text}
            </Text>
          </View>
        )}
      />

      <Text style={styles.sectionTitle}>Notes ({notes.length})</Text>
      <FlatList
        style={styles.flex}
        data={notes}
        keyExtractor={(n) => n.id}
        ListEmptyComponent={<Text style={styles.empty}>No notes yet — say “remember …”.</Text>}
        renderItem={({ item }) => (
          <View style={styles.noteRow}>
            <Text style={styles.noteText}>{item.text}</Text>
            <Pressable
              hitSlop={8}
              onPress={() => publishEdit({ action: 'delete', id: item.id })}
            >
              <Text style={styles.del}>✕</Text>
            </Pressable>
          </View>
        )}
      />

      <View style={styles.addRow}>
        <TextInput
          style={styles.input}
          placeholder="Type a note…"
          value={draft}
          onChangeText={setDraft}
          onSubmitEditing={addNote}
          returnKeyType="done"
        />
        <Pressable style={[styles.btn, styles.btnAdd]} onPress={addNote}>
          <Text style={styles.btnText}>Add</Text>
        </Pressable>
      </View>
    </View>
  );
}

// =====================================================================
// Root: handles the audio session + token fetch + connect lifecycle.
// =====================================================================
export default function App() {
  const [token, setToken] = useState<string | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const audioStarted = useRef(false);

  // Start the audio session once (mic capture + playback routing).
  useEffect(() => {
    (async () => {
      await AudioSession.startAudioSession();
      audioStarted.current = true;
    })();
    return () => {
      if (audioStarted.current) AudioSession.stopAudioSession();
    };
  }, []);

  const start = useCallback(async () => {
    setError(null);
    setConnecting(true);
    try {
      const res = await fetch(`${TOKEN_SERVER_URL}/token`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      if (!res.ok) throw new Error(`token server ${res.status}`);
      const data = await res.json();
      setUrl(data.url);
      setToken(data.token);
    } catch (e: any) {
      setError(e?.message ?? String(e));
      setConnecting(false);
    }
  }, []);

  const end = useCallback(() => {
    setToken(null);
    setUrl(null);
    setConnecting(false);
  }, []);

  const inRoom = useMemo(() => !!token && !!url, [token, url]);

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <StatusBar style="dark" />
      <Text style={styles.title}>Voice Memory Assistant</Text>

      {!inRoom ? (
        <View style={styles.center}>
          {connecting ? (
            <ActivityIndicator size="large" />
          ) : (
            <Pressable style={[styles.btn, styles.btnStart]} onPress={start}>
              <Text style={styles.btnText}>Start conversation</Text>
            </Pressable>
          )}
          {error ? <Text style={styles.error}>Error: {error}</Text> : null}
        </View>
      ) : (
        <LiveKitRoom
          serverUrl={url!}
          token={token!}
          connect
          audio
          video={false}
          onConnected={() => setConnecting(false)}
          onError={(e) => setError(e?.message ?? String(e))}
          onDisconnected={end}
        >
          <ConversationView onEnd={end} />
        </LiveKitRoom>
      )}
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#fff', paddingTop: 64, paddingHorizontal: 16 },
  flex: { flex: 1 },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 16 },
  title: { fontSize: 22, fontWeight: '700', marginBottom: 12 },
  sectionTitle: { fontSize: 16, fontWeight: '600', marginTop: 12, marginBottom: 6 },
  statusRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  pill: { paddingVertical: 6, paddingHorizontal: 14, borderRadius: 999 },
  pillOk: { backgroundColor: '#dcfce7' },
  pillWarn: { backgroundColor: '#fef9c3' },
  pillText: { fontWeight: '600' },
  btn: { paddingVertical: 12, paddingHorizontal: 18, borderRadius: 12, alignItems: 'center' },
  btnStart: { backgroundColor: '#2563eb' },
  btnEnd: { backgroundColor: '#dc2626' },
  btnAdd: { backgroundColor: '#2563eb' },
  btnText: { color: 'white', fontWeight: '700', fontSize: 16 },
  bubble: {
    borderRadius: 12, paddingVertical: 8, paddingHorizontal: 12,
    marginVertical: 4, maxWidth: '85%',
  },
  bubbleUser: { backgroundColor: '#dbeafe', alignSelf: 'flex-end' },
  bubbleAgent: { backgroundColor: '#f1f5f9', alignSelf: 'flex-start' },
  bubbleRole: { fontSize: 11, fontWeight: '700', color: '#64748b', marginBottom: 2 },
  bubbleText: { fontSize: 15, color: '#0f172a' },
  bubbleInterim: { color: '#64748b', fontStyle: 'italic' },
  noteRow: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    borderWidth: 1, borderColor: '#e5e7eb', borderRadius: 10, padding: 12, marginVertical: 4,
  },
  noteText: { flex: 1, fontSize: 15, marginRight: 8 },
  del: { color: '#dc2626', fontSize: 18, fontWeight: '700' },
  empty: { color: '#888', fontStyle: 'italic', marginTop: 8 },
  addRow: { flexDirection: 'row', gap: 8, paddingVertical: 10, alignItems: 'center' },
  input: {
    flex: 1, borderWidth: 1, borderColor: '#cbd5e1', borderRadius: 10,
    paddingHorizontal: 12, paddingVertical: 10, fontSize: 15,
  },
  error: { color: '#dc2626', textAlign: 'center' },
});
