// Shared dark theme tokens for Voice Memory Assistant v3.0.
// One source of truth for colors, spacing, radii, and the type scale so the two
// screens feel like one cohesive, modern app (not a default template).

export const colors = {
  bg: '#0B0F14', // app background (near-black navy)
  bgElevated: '#11161D', // headers / sheets
  card: '#161C24', // doc + entry cards
  cardPressed: '#1C232D',
  border: '#232B36',
  borderSoft: '#1B222B',

  text: '#E6EDF3', // primary text
  textDim: '#9AA7B4', // secondary text
  textFaint: '#5E6B79', // hints / placeholders

  accent: '#6E8BFF', // indigo — primary actions
  accentSoft: '#1E2A4A', // tinted accent surface
  accentText: '#C9D4FF',

  listening: '#3FB950', // mic on
  listeningSoft: '#10261A',
  idle: '#D29922', // mic off / connecting
  idleSoft: '#2A2310',

  danger: '#F85149',
  dangerSoft: '#2A1517',

  bubbleUser: '#1E2A4A',
  bubbleAgent: '#1A2129',
} as const;

export const space = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
} as const;

export const radius = {
  sm: 10,
  md: 14,
  lg: 18,
  pill: 999,
} as const;

export const type = {
  display: { fontSize: 28, fontWeight: '800' as const, letterSpacing: 0.2 },
  title: { fontSize: 22, fontWeight: '700' as const },
  section: { fontSize: 14, fontWeight: '700' as const, letterSpacing: 0.8 },
  body: { fontSize: 16, fontWeight: '400' as const },
  bodyStrong: { fontSize: 16, fontWeight: '600' as const },
  small: { fontSize: 13, fontWeight: '400' as const },
  label: { fontSize: 12, fontWeight: '700' as const, letterSpacing: 0.6 },
} as const;
