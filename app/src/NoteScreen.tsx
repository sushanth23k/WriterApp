// NOTE state — an isolated conversation about ONE doc. The user just talks and the
// assistant notes things down; the room is scoped to this doc only (enforced by the
// backend). Title/description/entries are loaded and edited DIRECTLY over the REST API
// (scoped to the signed-in user); the data channel only carries live updates while the
// voice agent is mid-call.

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  FlatList,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { useRoomContext } from '@livekit/react-native';

import {
  addEntry as apiAddEntry,
  deleteEntry as apiDeleteEntry,
  getDoc,
  updateDoc,
  updateEntry,
} from './api';
import { UnauthorizedError } from './auth';
import { colors, radius, space, type } from './theme';
import { TOPICS, type Entry, type FullDoc } from './types';
import { useConnState, useDataTopic, useMic, useTranscript } from './livekit';
import { CommandsHint, MicButton, SectionLabel, StatusPill, TranscriptView } from './ui';

export function NoteScreen({
  token,
  docId,
  initialTitle,
  onBack,
  onSignOut,
}: {
  token: string;
  docId: string;
  initialTitle: string;
  onBack: () => void;
  onSignOut: () => void;
}) {
  const room = useRoomContext();
  const [doc, setDoc] = useState<FullDoc | null>(null);
  const [title, setTitle] = useState(initialTitle);
  const [desc, setDesc] = useState('');
  const editingTitle = useRef(false);
  const editingDesc = useRef(false);

  const [draft, setDraft] = useState('');
  const [editId, setEditId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');

  const connState = useConnState(room);
  const connected = connState === 'connected';
  const [micOn, toggleMic, setMic] = useMic(room);
  const transcript = useTranscript(room);

  // Apply a fetched doc to local state, respecting any in-progress field edits.
  const applyDoc = useCallback((d: FullDoc) => {
    setDoc(d);
    if (!editingTitle.current) setTitle(d.title);
    if (!editingDesc.current) setDesc(d.description);
  }, []);

  // Run a REST mutation, then refetch the doc so local state matches the DB. An
  // expired session signs the user out.
  const withReload = useCallback(
    async (op: () => Promise<unknown>) => {
      try {
        await op();
        applyDoc(await getDoc(token, docId));
      } catch (e) {
        if (e instanceof UnauthorizedError) onSignOut();
      }
    },
    [token, docId, applyDoc, onSignOut],
  );

  // Initial load: fetch this doc (with entries) directly over REST.
  useEffect(() => {
    (async () => {
      try {
        applyDoc(await getDoc(token, docId));
      } catch (e) {
        if (e instanceof UnauthorizedError) onSignOut();
      }
    })();
  }, [token, docId, applyDoc, onSignOut]);

  // Live updates while a voice call is active: the note agent broadcasts this doc
  // (with entries) after a voice-driven change.
  useDataTopic(room, TOPICS.doc, (msg) => {
    if (msg?.type !== 'doc' || !msg.doc) return;
    applyDoc({ ...msg.doc, entries: msg.doc.entries ?? [] });
  });

  // Voice state-movement control: "go back" -> MAIN; "stop" -> stop listening
  // (mute the mic in place, same as the on-screen Stop button — room stays up).
  useDataTopic(room, TOPICS.control, (msg) => {
    if (msg?.type !== 'control') return;
    if (msg.action === 'go_back') onBack();
    else if (msg.action === 'stop') setMic(false);
  });

  const saveMeta = useCallback(
    (patch: { title?: string; description?: string }) =>
      withReload(() => updateDoc(token, docId, patch)),
    [withReload, token, docId],
  );

  const addEntry = useCallback(() => {
    const t = draft.trim();
    if (!t) return;
    setDraft('');
    withReload(() => apiAddEntry(token, docId, t));
  }, [draft, withReload, token, docId]);

  const saveEntry = useCallback(() => {
    const id = editId;
    const text = editText.trim();
    setEditId(null);
    setEditText('');
    if (id) withReload(() => updateEntry(token, docId, id, text));
  }, [editId, editText, withReload, token, docId]);

  const deleteEntry = useCallback(
    (id: string) => withReload(() => apiDeleteEntry(token, docId, id)),
    [withReload, token, docId],
  );

  const entries: Entry[] = doc?.entries ?? [];

  return (
    <View style={styles.flex}>
      <View style={styles.topRow}>
        <Pressable hitSlop={10} onPress={onBack} style={styles.back}>
          <Text style={styles.backText}>‹  Notes</Text>
        </Pressable>
        <View style={styles.topRight}>
          <StatusPill connected={connected} micOn={micOn} />
          <CommandsHint showGoBack />
        </View>
      </View>

      <TextInput
        style={styles.titleInput}
        value={title}
        onChangeText={setTitle}
        onFocus={() => (editingTitle.current = true)}
        onEndEditing={() => {
          editingTitle.current = false;
          if (doc && title.trim() !== doc.title) saveMeta({ title: title.trim() });
        }}
        placeholder="Untitled note"
        placeholderTextColor={colors.textFaint}
      />
      <TextInput
        style={styles.descInput}
        value={desc}
        onChangeText={setDesc}
        onFocus={() => (editingDesc.current = true)}
        onEndEditing={() => {
          editingDesc.current = false;
          if (doc && desc.trim() !== doc.description) saveMeta({ description: desc.trim() });
        }}
        placeholder="Add a description"
        placeholderTextColor={colors.textFaint}
        multiline
      />

      <View style={styles.entriesHeader}>
        <SectionLabel>
          {entries.length} {entries.length === 1 ? 'ENTRY' : 'ENTRIES'}
        </SectionLabel>
        <MicButton micOn={micOn} onToggle={toggleMic} disabled={!connected} />
      </View>

      <FlatList
        style={styles.flex}
        data={entries}
        keyExtractor={(e) => e.id}
        showsVerticalScrollIndicator={false}
        contentContainerStyle={entries.length === 0 && styles.emptyWrap}
        ListEmptyComponent={
          <Text style={styles.empty}>
            Start talking and I’ll note things down here — or type below.
          </Text>
        }
        renderItem={({ item }) =>
          editId === item.id ? (
            <View style={[styles.entry, styles.entryEditing]}>
              <TextInput
                style={styles.entryEdit}
                value={editText}
                onChangeText={setEditText}
                onSubmitEditing={saveEntry}
                onBlur={saveEntry}
                autoFocus
                multiline
                returnKeyType="done"
              />
            </View>
          ) : (
            <Pressable
              style={({ pressed }) => [styles.entry, pressed && styles.entryPressed]}
              onPress={() => {
                setEditId(item.id);
                setEditText(item.text);
              }}
            >
              <Text style={styles.entryText}>{item.text}</Text>
              <Pressable hitSlop={10} onPress={() => deleteEntry(item.id)} style={styles.entryDel}>
                <Text style={styles.entryDelText}>✕</Text>
              </Pressable>
            </Pressable>
          )
        }
      />

      <TranscriptView transcript={transcript} />

      <View style={styles.addRow}>
        <TextInput
          style={styles.addInput}
          placeholder="Type an entry…"
          placeholderTextColor={colors.textFaint}
          value={draft}
          onChangeText={setDraft}
          onSubmitEditing={addEntry}
          returnKeyType="done"
        />
        <Pressable
          style={({ pressed }) => [styles.addBtn, pressed && { opacity: 0.85 }]}
          onPress={addEntry}
        >
          <Text style={styles.addBtnText}>Add</Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  topRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: space.md,
  },
  topRight: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  back: { paddingVertical: 4, paddingRight: space.md },
  backText: { ...type.bodyStrong, color: colors.accent },
  titleInput: {
    ...type.display,
    color: colors.text,
    paddingVertical: space.xs,
  },
  descInput: {
    ...type.body,
    color: colors.textDim,
    paddingVertical: space.xs,
    marginBottom: space.lg,
  },
  entriesHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: space.sm,
  },
  emptyWrap: { flexGrow: 1, justifyContent: 'center' },
  empty: {
    ...type.body,
    color: colors.textFaint,
    textAlign: 'center',
    lineHeight: 22,
    paddingHorizontal: space.lg,
  },
  entry: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingVertical: space.md,
    paddingHorizontal: space.lg,
    marginBottom: space.sm,
  },
  entryPressed: { backgroundColor: colors.cardPressed },
  entryEditing: { borderColor: colors.accent },
  entryText: { ...type.body, color: colors.text, flex: 1, lineHeight: 21 },
  entryEdit: { ...type.body, color: colors.text, flex: 1, padding: 0 },
  entryDel: { paddingLeft: space.md },
  entryDelText: { color: colors.textFaint, fontSize: 15, fontWeight: '700' },
  addRow: {
    flexDirection: 'row',
    gap: space.sm,
    alignItems: 'center',
    paddingTop: space.sm,
  },
  addInput: {
    flex: 1,
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
    color: colors.text,
    ...type.body,
  },
  addBtn: {
    backgroundColor: colors.accent,
    borderRadius: radius.md,
    paddingVertical: space.md,
    paddingHorizontal: space.lg,
  },
  addBtnText: { ...type.bodyStrong, color: '#0B0F14' },
});
