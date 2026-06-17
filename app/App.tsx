// Voice Memory Assistant v3.0 — two-state app.
//
//   MAIN  : browse / navigate / create note docs (NavigatorAgent, docs list only)
//   NOTE  : an isolated conversation about ONE doc (NoteAgent, that doc only)
//
// Each state mounts its OWN <LiveKitRoom> (a fresh "main" room or a per-doc
// "note-{id}" room) — switching states fully tears down the previous room, which
// is what enforces context isolation on the client side too. The proven v2.0
// audio session + transcription + data-channel logic is reused via src/livekit.ts.

import { StatusBar } from 'expo-status-bar';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { AudioSession, LiveKitRoom } from '@livekit/react-native';

import { fetchToken } from './src/api';
import { UnauthorizedError, useAuth } from './src/auth';
import { LoginScreen } from './src/LoginScreen';
import { MainScreen } from './src/MainScreen';
import { NoteScreen } from './src/NoteScreen';
import { colors, radius, space, type } from './src/theme';
import type { Screen } from './src/types';

type Conn = { url: string; token: string };

export default function App() {
  const { token, loading, signIn, signOut } = useAuth();

  // Until auth resolves, show nothing/spinner; gate the whole app on a session.
  if (loading) {
    return (
      <View style={[styles.container, styles.center]}>
        <ActivityIndicator size="large" color={colors.accent} />
      </View>
    );
  }
  if (!token) {
    return <LoginScreen onSignIn={signIn} />;
  }
  return <AuthedApp token={token} onSignOut={signOut} />;
}

function AuthedApp({
  token,
  onSignOut,
}: {
  token: string;
  onSignOut: () => void;
}) {
  const [screen, setScreen] = useState<Screen>({ mode: 'main' });
  const [conn, setConn] = useState<Conn | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
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

  // (Re)connect whenever the screen (or a manual retry) changes. A 401 here means
  // the session expired -> sign out.
  useEffect(() => {
    let cancelled = false;
    setConn(null);
    setError(null);
    (async () => {
      try {
        const data =
          screen.mode === 'note'
            ? await fetchToken('note', token, screen.docId)
            : await fetchToken('main', token);
        if (!cancelled) setConn({ url: data.url, token: data.token });
      } catch (e: any) {
        if (cancelled) return;
        if (e instanceof UnauthorizedError) {
          onSignOut();
          return;
        }
        setError(e?.message ?? String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [screen, retry, token, onSignOut]);

  const goNote = useCallback(
    (docId: string, title: string) => setScreen({ mode: 'note', docId, title }),
    [],
  );
  const goMain = useCallback(() => setScreen({ mode: 'main' }), []);

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <StatusBar style="light" />

      {error ? (
        <View style={styles.center}>
          <Text style={styles.errorTitle}>Couldn’t connect</Text>
          <Text style={styles.errorMsg}>{error}</Text>
          <Pressable
            style={styles.retryBtn}
            onPress={() => setRetry((r) => r + 1)}
          >
            <Text style={styles.retryText}>Try again</Text>
          </Pressable>
        </View>
      ) : !conn ? (
        <View style={styles.center}>
          <ActivityIndicator size="large" color={colors.accent} />
          <Text style={styles.connecting}>
            {screen.mode === 'note' ? 'Opening note…' : 'Connecting…'}
          </Text>
        </View>
      ) : (
        <LiveKitRoom
          // token differs per fetch -> remounts (and disconnects the old room) on
          // every state change, guaranteeing a clean per-state room.
          key={conn.token}
          serverUrl={conn.url}
          token={conn.token}
          connect
          audio
          video={false}
          onError={(e) => setError(e?.message ?? String(e))}
        >
          {screen.mode === 'main' ? (
            <MainScreen onNavigate={goNote} onSignOut={onSignOut} />
          ) : (
            <NoteScreen
              docId={screen.docId}
              initialTitle={screen.title}
              onBack={goMain}
            />
          )}
        </LiveKitRoom>
      )}
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.bg,
    paddingTop: 60,
    paddingHorizontal: 20,
    paddingBottom: 12,
  },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: space.lg },
  connecting: { ...type.body, color: colors.textDim, textAlign: 'center' },
  errorTitle: { ...type.title, color: colors.text },
  errorMsg: {
    ...type.body,
    color: colors.danger,
    textAlign: 'center',
    paddingHorizontal: space.xl,
  },
  retryBtn: {
    backgroundColor: colors.accent,
    borderRadius: radius.pill,
    paddingVertical: 12,
    paddingHorizontal: space.xl,
  },
  retryText: { ...type.bodyStrong, color: colors.bg },
});
