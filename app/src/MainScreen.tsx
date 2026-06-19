// MAIN state — browse / navigate / create. The "navigator" conversation lets the
// user pick a doc by voice (title/id/description) or create one; tapping a card
// works too. Notes are loaded and mutated DIRECTLY over the REST API (scoped to the
// signed-in user); the data channel is only used for live updates while the voice
// agent is mid-call.

import { useCallback, useEffect, useState } from 'react';
import {
  FlatList,
  Modal,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { useRoomContext } from '@livekit/react-native';

import { createDoc, deleteDoc as apiDeleteDoc, listDocs } from './api';
import { UnauthorizedError } from './auth';
import { colors, radius, space, type } from './theme';
import { TOPICS, type Doc } from './types';
import { useConnState, useDataTopic, useMic, useTranscript } from './livekit';
import { CommandsHint, MicButton, SectionLabel, StatusPill, TranscriptView } from './ui';

export function MainScreen({
  token,
  onNavigate,
  onSignOut,
}: {
  token: string;
  onNavigate: (docId: string, title: string) => void;
  onSignOut: () => void;
}) {
  const room = useRoomContext();
  const [docs, setDocs] = useState<Doc[]>([]);
  const [creating, setCreating] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [newDesc, setNewDesc] = useState('');

  const connState = useConnState(room);
  const connected = connState === 'connected';
  const [micOn, toggleMic, setMic] = useMic(room);
  const transcript = useTranscript(room);

  // Load the user's docs directly over REST (an expired session signs them out).
  const loadDocs = useCallback(async () => {
    try {
      setDocs(await listDocs(token));
    } catch (e) {
      if (e instanceof UnauthorizedError) onSignOut();
    }
  }, [token, onSignOut]);

  useEffect(() => {
    loadDocs();
  }, [loadDocs]);

  // Live updates while a voice call is active: the agent broadcasts the docs list
  // after a voice-driven change so the list stays current without a manual refetch.
  useDataTopic(room, TOPICS.docs, (msg) => {
    if (msg?.type !== 'docs' || !Array.isArray(msg.docs)) return;
    setDocs(msg.docs);
  });

  // Voice-driven navigation: the agent resolved/created a doc.
  useDataTopic(room, TOPICS.navigate, (msg) => {
    if (msg?.type === 'navigate' && msg.doc_id) {
      onNavigate(msg.doc_id, msg.title ?? '');
    }
  });

  // Voice state-movement control. In MAIN only "stop" applies: stop listening by
  // muting the mic in place (same as the on-screen Stop button).
  useDataTopic(room, TOPICS.control, (msg) => {
    if (msg?.type === 'control' && msg.action === 'stop') setMic(false);
  });

  const submitCreate = useCallback(async () => {
    const title = newTitle.trim();
    if (!title) return;
    setNewTitle('');
    setNewDesc('');
    setCreating(false);
    try {
      const doc = await createDoc(token, title, newDesc.trim());
      await loadDocs();
      onNavigate(doc.id, doc.title); // open the freshly-created note
    } catch (e) {
      if (e instanceof UnauthorizedError) onSignOut();
    }
  }, [newTitle, newDesc, token, loadDocs, onNavigate, onSignOut]);

  const deleteDoc = useCallback(
    async (id: string) => {
      try {
        await apiDeleteDoc(token, id);
        await loadDocs();
      } catch (e) {
        if (e instanceof UnauthorizedError) onSignOut();
      }
    },
    [token, loadDocs, onSignOut],
  );

  return (
    <View style={styles.flex}>
      <View style={styles.header}>
        <View>
          <Text style={styles.kicker}>DROPNOTE</Text>
          <Text style={styles.h1}>Your notes</Text>
        </View>
        <View style={styles.headerRight}>
          <View style={styles.headerRightTop}>
            <StatusPill connected={connected} micOn={micOn} />
            <CommandsHint showGoBack={false} />
          </View>
          <Pressable hitSlop={8} onPress={onSignOut} style={styles.logoutBtn}>
            <Text style={styles.logoutText}>Log out</Text>
          </Pressable>
        </View>
      </View>

      <View style={styles.controls}>
        <MicButton micOn={micOn} onToggle={toggleMic} disabled={!connected} />
        <Pressable
          style={({ pressed }) => [styles.newBtn, pressed && { opacity: 0.85 }]}
          onPress={() => setCreating(true)}
        >
          <Text style={styles.newBtnText}>＋  New note</Text>
        </Pressable>
      </View>

      <SectionLabel>
        {docs.length} {docs.length === 1 ? 'NOTE' : 'NOTES'}
      </SectionLabel>

      <FlatList
        style={styles.flex}
        data={docs}
        keyExtractor={(d) => d.id}
        showsVerticalScrollIndicator={false}
        contentContainerStyle={docs.length === 0 && styles.emptyWrap}
        ListEmptyComponent={
          <Text style={styles.empty}>
            No notes yet. Say “make a new note about…”, or tap ＋ New note.
          </Text>
        }
        renderItem={({ item }) => (
          <Pressable
            style={({ pressed }) => [styles.card, pressed && styles.cardPressed]}
            onPress={() => onNavigate(item.id, item.title)}
          >
            <View style={styles.flex}>
              <Text style={styles.cardTitle} numberOfLines={1}>
                {item.title || 'Untitled note'}
              </Text>
              {item.description ? (
                <Text style={styles.cardDesc} numberOfLines={1}>
                  {item.description}
                </Text>
              ) : (
                <Text style={[styles.cardDesc, styles.cardDescFaint]} numberOfLines={1}>
                  No description
                </Text>
              )}
            </View>
            <Pressable
              hitSlop={10}
              onPress={() => deleteDoc(item.id)}
              style={styles.cardDel}
            >
              <Text style={styles.cardDelText}>✕</Text>
            </Pressable>
          </Pressable>
        )}
      />

      <TranscriptView transcript={transcript} />

      {/* Create-note sheet */}
      <Modal
        transparent
        visible={creating}
        animationType="fade"
        onRequestClose={() => setCreating(false)}
      >
        <Pressable style={styles.backdrop} onPress={() => setCreating(false)}>
          <Pressable style={styles.sheet} onPress={() => {}}>
            <Text style={styles.sheetTitle}>New note</Text>
            <TextInput
              style={styles.input}
              placeholder="Title"
              placeholderTextColor={colors.textFaint}
              value={newTitle}
              onChangeText={setNewTitle}
              autoFocus
              returnKeyType="next"
            />
            <TextInput
              style={[styles.input, styles.inputMulti]}
              placeholder="Short description (optional)"
              placeholderTextColor={colors.textFaint}
              value={newDesc}
              onChangeText={setNewDesc}
              multiline
            />
            <View style={styles.sheetRow}>
              <Pressable
                style={[styles.sheetBtn, styles.sheetCancel]}
                onPress={() => setCreating(false)}
              >
                <Text style={styles.sheetCancelText}>Cancel</Text>
              </Pressable>
              <Pressable
                style={[styles.sheetBtn, styles.sheetCreate]}
                onPress={submitCreate}
              >
                <Text style={styles.sheetCreateText}>Create & open</Text>
              </Pressable>
            </View>
          </Pressable>
        </Pressable>
      </Modal>
    </View>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  header: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    marginBottom: space.lg,
  },
  headerRight: { alignItems: 'flex-end', gap: space.sm },
  headerRightTop: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  logoutBtn: { paddingVertical: 2, paddingHorizontal: 4 },
  logoutText: { ...type.label, color: colors.textDim },
  kicker: { ...type.label, color: colors.accent, marginBottom: 2 },
  h1: { ...type.display, color: colors.text },
  controls: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    marginBottom: space.xl,
  },
  newBtn: {
    flex: 1,
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.pill,
    paddingVertical: 11,
    alignItems: 'center',
  },
  newBtnText: { ...type.bodyStrong, color: colors.text },
  emptyWrap: { flexGrow: 1, justifyContent: 'center' },
  empty: {
    ...type.body,
    color: colors.textFaint,
    textAlign: 'center',
    lineHeight: 22,
    paddingHorizontal: space.lg,
  },
  card: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.lg,
    paddingVertical: space.lg,
    paddingHorizontal: space.lg,
    marginBottom: space.md,
  },
  cardPressed: { backgroundColor: colors.cardPressed },
  cardTitle: { ...type.bodyStrong, color: colors.text, marginBottom: 3 },
  cardDesc: { ...type.small, color: colors.textDim },
  cardDescFaint: { color: colors.textFaint, fontStyle: 'italic' },
  cardDel: {
    width: 32,
    height: 32,
    alignItems: 'center',
    justifyContent: 'center',
    marginLeft: space.sm,
  },
  cardDelText: { color: colors.textFaint, fontSize: 16, fontWeight: '700' },
  backdrop: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.6)',
    justifyContent: 'center',
    paddingHorizontal: space.xl,
  },
  sheet: {
    backgroundColor: colors.bgElevated,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: space.xl,
    gap: space.md,
  },
  sheetTitle: { ...type.title, color: colors.text, marginBottom: space.xs },
  input: {
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
    color: colors.text,
    ...type.body,
  },
  inputMulti: { minHeight: 64, textAlignVertical: 'top' },
  sheetRow: { flexDirection: 'row', gap: space.md, marginTop: space.xs },
  sheetBtn: {
    flex: 1,
    borderRadius: radius.pill,
    paddingVertical: 12,
    alignItems: 'center',
  },
  sheetCancel: { backgroundColor: colors.card, borderWidth: 1, borderColor: colors.border },
  sheetCancelText: { ...type.bodyStrong, color: colors.textDim },
  sheetCreate: { backgroundColor: colors.accent },
  sheetCreateText: { ...type.bodyStrong, color: '#0B0F14' },
});
