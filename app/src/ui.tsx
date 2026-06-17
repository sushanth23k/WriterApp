// Shared presentational components for a consistent, modern look across screens.

import { FlatList, Modal, Pressable, StyleSheet, Text, View } from 'react-native';
import { useEffect, useRef, useState } from 'react';

import { colors, radius, space, type } from './theme';
import type { TranscriptItem } from './types';

// A small corner badge that reveals the available voice commands in a popup.
// "Stop it" is always available; "Go back" only inside a note (showGoBack).
export function CommandsHint({ showGoBack }: { showGoBack: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Pressable
        hitSlop={8}
        onPress={() => setOpen(true)}
        style={({ pressed }) => [styles.hintBadge, pressed && { opacity: 0.7 }]}
      >
        <Text style={styles.hintBadgeText}>ⓘ</Text>
      </Pressable>
      <Modal
        transparent
        visible={open}
        animationType="fade"
        onRequestClose={() => setOpen(false)}
      >
        <Pressable style={styles.hintBackdrop} onPress={() => setOpen(false)}>
          <Pressable style={styles.hintCard} onPress={() => {}}>
            <Text style={styles.hintTitle}>Voice commands</Text>
            {showGoBack ? (
              <View style={styles.hintRow}>
                <Text style={styles.hintCmd}>“Go back”</Text>
                <Text style={styles.hintDesc}>return to your notes</Text>
              </View>
            ) : null}
            <View style={styles.hintRow}>
              <Text style={styles.hintCmd}>“Stop it”</Text>
              <Text style={styles.hintDesc}>stop listening (tap Start to resume)</Text>
            </View>
          </Pressable>
        </Pressable>
      </Modal>
    </>
  );
}

// Connection / listening status chip.
export function StatusPill({
  connected,
  micOn,
}: {
  connected: boolean;
  micOn: boolean;
}) {
  const label = !connected ? 'Connecting…' : micOn ? 'Listening' : 'Paused';
  const tone = !connected ? colors.idle : micOn ? colors.listening : colors.textFaint;
  const bg = !connected ? colors.idleSoft : micOn ? colors.listeningSoft : colors.borderSoft;
  return (
    <View style={[styles.pill, { backgroundColor: bg }]}>
      <View style={[styles.dot, { backgroundColor: tone }]} />
      <Text style={[styles.pillText, { color: tone }]}>{label}</Text>
    </View>
  );
}

// The start/stop-conversation control (mic toggle).
export function MicButton({
  micOn,
  onToggle,
  disabled,
}: {
  micOn: boolean;
  onToggle: () => void;
  disabled?: boolean;
}) {
  return (
    <Pressable
      onPress={onToggle}
      disabled={disabled}
      style={({ pressed }) => [
        styles.mic,
        micOn ? styles.micOn : styles.micOff,
        pressed && { opacity: 0.85 },
        disabled && { opacity: 0.4 },
      ]}
    >
      <Text style={[styles.micText, { color: micOn ? colors.danger : colors.accentText }]}>
        {micOn ? '◼  Stop' : '🎙  Start'}
      </Text>
    </Pressable>
  );
}

// Live conversation transcript (auto-scrolls to the latest line).
export function TranscriptView({ transcript }: { transcript: TranscriptItem[] }) {
  const ref = useRef<FlatList<TranscriptItem>>(null);
  useEffect(() => {
    if (transcript.length) ref.current?.scrollToEnd({ animated: true });
  }, [transcript.length]);

  if (!transcript.length) return null;
  return (
    <FlatList
      ref={ref}
      data={transcript}
      style={styles.transcript}
      keyExtractor={(t) => t.id}
      showsVerticalScrollIndicator={false}
      onContentSizeChange={() => ref.current?.scrollToEnd({ animated: true })}
      renderItem={({ item }) => (
        <View
          style={[
            styles.bubble,
            item.role === 'user' ? styles.bubbleUser : styles.bubbleAgent,
          ]}
        >
          <Text style={styles.bubbleText}>{item.text}</Text>
        </View>
      )}
    />
  );
}

export function SectionLabel({ children }: { children: React.ReactNode }) {
  return <Text style={styles.sectionLabel}>{children}</Text>;
}

const styles = StyleSheet.create({
  pill: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    paddingVertical: 6,
    paddingHorizontal: 12,
    borderRadius: radius.pill,
  },
  dot: { width: 8, height: 8, borderRadius: 4 },
  pillText: { ...type.label },
  mic: {
    paddingVertical: 10,
    paddingHorizontal: 18,
    borderRadius: radius.pill,
    borderWidth: 1,
  },
  micOn: { backgroundColor: colors.dangerSoft, borderColor: colors.danger },
  micOff: { backgroundColor: colors.accentSoft, borderColor: colors.accent },
  micText: { ...type.bodyStrong },
  transcript: {
    maxHeight: 150,
    marginTop: space.sm,
  },
  bubble: {
    borderRadius: radius.md,
    paddingVertical: 8,
    paddingHorizontal: 12,
    marginVertical: 3,
    maxWidth: '88%',
  },
  bubbleUser: { backgroundColor: colors.bubbleUser, alignSelf: 'flex-end' },
  bubbleAgent: { backgroundColor: colors.bubbleAgent, alignSelf: 'flex-start' },
  bubbleText: { ...type.small, color: colors.text, lineHeight: 19 },
  sectionLabel: {
    ...type.section,
    color: colors.textFaint,
    marginBottom: space.sm,
  },
  hintBadge: {
    width: 26,
    height: 26,
    borderRadius: 13,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
  },
  hintBadgeText: { color: colors.textDim, fontSize: 14, fontWeight: '700' },
  hintBackdrop: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.6)',
    justifyContent: 'center',
    paddingHorizontal: space.xl,
  },
  hintCard: {
    backgroundColor: colors.bgElevated,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: space.xl,
    gap: space.md,
  },
  hintTitle: { ...type.title, color: colors.text, marginBottom: space.xs },
  hintRow: { flexDirection: 'row', alignItems: 'baseline', gap: space.sm },
  hintCmd: { ...type.bodyStrong, color: colors.accentText },
  hintDesc: { ...type.small, color: colors.textDim },
});
