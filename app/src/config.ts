// Token server base URL.
//
// LOCAL DEV (default): a physical iPhone cannot reach the Mac via "localhost" —
// it must use the Mac's LAN IP (the same address the local LiveKit server
// advertises). Update this if your Mac's IP changes (ipconfig getifaddr en0).
export const TOKEN_SERVER_URL = 'http://192.168.1.104:8080';

// PRODUCTION (single GCE VM, TLS via Caddy): point at your domain instead. iOS
// App Transport Security requires https/wss in production, so use the https URL
// here — the token server returns the matching wss:// LiveKit URL (the VM's
// LIVEKIT_URL in backend/.env). Swap the line above for:
//   export const TOKEN_SERVER_URL = 'https://api.example.com';
