// Token server base URL.
//
// A physical iPhone cannot reach the Mac via "localhost" or "0.0.0.0" — it must use
// the Mac's LAN IP (the same address the local LiveKit server advertises, and what
// backend/docker-compose.yml documents). Update this if your Mac's IP changes
// (find it with: ipconfig getifaddr en0).
//
// NOTE: this MUST be named TOKEN_SERVER_URL — api.ts and auth.ts import it by that
// name. If it's missing, those imports are `undefined` and every request resolves to
// "undefined/token" against the Expo dev server, which 404s.
// export const TOKEN_SERVER_URL = 'http://192.168.1.104:8080';
export const TOKEN_SERVER_URL = 'http://34.57.95.145:8080';