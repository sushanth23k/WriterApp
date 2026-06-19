// Token server base URL.
//
// A physical iPhone cannot reach the Mac via "localhost" — it must use the Mac's
// LAN IP (the same address the local LiveKit server advertises). Update this if
// your Mac's IP changes (find it with: ipconfig getifaddr en0).
export const TOKEN_SERVER_URL = 'http://34.57.95.145:8080';

// export const EXPO_DEV_SERVER_URL = 'https://134.57.95.145:8080';