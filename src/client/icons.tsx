import type { ReactNode } from 'react'

function Svg({ children, size = 16 }: { children: ReactNode; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{children}</svg>
}
export const IconSearch = () => <Svg><circle cx="7" cy="7" r="4.5" /><path d="m10.5 10.5 3 3" /></Svg>
export const IconClose = () => <Svg><path d="m4 4 8 8M12 4l-8 8" /></Svg>
export const IconBack = () => <Svg><path d="M10 3.5 5.5 8l4.5 4.5" /></Svg>
export const IconPlus = () => <Svg><path d="M8 3.5v9M3.5 8h9" /></Svg>
export const IconMinus = () => <Svg><path d="M3.5 8h9" /></Svg>
export const IconReset = () => <Svg><path d="M3.2 6.2A5 5 0 1 1 3 8.8" /><path d="M3 3v3.4h3.4" /></Svg>
export const IconPause = () => <Svg><path d="M6 4v8M10 4v8" /></Svg>
export const IconPlay = () => <Svg><path d="M5.5 3.8v8.4L12 8z" /></Svg>
export const IconLock = () => <Svg size={20}><rect x="3.5" y="7" width="9" height="6.5" rx="1.5" /><path d="M5.5 7V5.2a2.5 2.5 0 0 1 5 0V7" /></Svg>
export const IconAlert = () => <Svg size={20}><path d="M8 2.5 14 13H2z" /><path d="M8 6.5v3" /><path d="M8 11.4v.1" /></Svg>
export const IconEdit = () => <Svg><path d="M10.5 3 13 5.5 6 12.5H3.5V10z" /></Svg>
export const IconForget = () => <Svg><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5" /></Svg>
export const IconArrow = () => <Svg><path d="M6 3.5 10.5 8 6 12.5" /></Svg>

/** Sidebar glyph: a soma with apical, lateral and basal processes. */
export function NeuronGlyph({ size }: { size: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
    <circle cx="12" cy="12.5" r="2.6" />
    <path d="M12 9.9V3.5M12 6.2 8.8 3.8M12 6.2l3.2-2.4" />
    <path d="M9.6 11.6 4 9.6M14.4 11.6l5.6-2" />
    <path d="M10.4 14.6 6.5 20M13.6 14.6l3.9 5.4M12 15.1V21" />
  </svg>
}
