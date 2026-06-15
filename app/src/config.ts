// Token server base URL.
//
// A physical iPhone cannot reach the Mac via "localhost" — it must use the Mac's
// LAN IP (the same address the local LiveKit server advertises). Update this if
// your Mac's IP changes (find it with: ipconfig getifaddr en0).
export const TOKEN_SERVER_URL = 'http://192.168.1.104:8080';
