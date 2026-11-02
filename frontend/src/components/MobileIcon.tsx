import type { SVGProps } from 'react'

export type MobileIconName = 'home' | 'spark' | 'chart' | 'user' | 'bell' | 'arrow' | 'back' | 'eye' | 'eye-off' | 'transfer' | 'receipt' | 'camera' | 'wallet' | 'shield' | 'memory' | 'tasks' | 'cards' | 'send' | 'clock' | 'settings' | 'logout' | 'refresh' | 'check' | 'menu' | 'chevron' | 'info' | 'grid' | 'plus' | 'voice' | 'headset' | 'chat'

const paths: Record<MobileIconName, JSX.Element> = {
  grid: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></>,
  plus: <><circle cx="12" cy="12" r="10"/><path d="M12 6v12M6 12h12"/></>,
  voice: <><circle cx="12" cy="12" r="10"/><path d="M9 9a4 4 0 0 1 0 6m4-9a8 8 0 0 1 0 12M6 12h.1"/></>,
  headset: <><path d="M4 14v-3a8 8 0 0 1 16 0v3m0 3v1a3 3 0 0 1-3 3h-4"/><rect x="2" y="11" width="4" height="7" rx="2"/><rect x="18" y="11" width="4" height="7" rx="2"/></>,
  chat: <><path d="M21 11a9 9 0 0 1-9 9 12 12 0 0 1-4-1l-5 2 1-5a9 9 0 1 1 17-5Z"/><path d="M7 11h.1m5 0h.1m5 0h.1"/></>,
  home: <><path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z"/></>,
  spark: <><path d="m12 2 1.8 6.2L20 10l-6.2 1.8L12 18l-1.8-6.2L4 10l6.2-1.8Z"/><path d="m19 16 .9 2.6L22 19l-2.1.9L19 22l-.9-2.1L16 19l2.1-.4Z"/></>,
  chart: <><path d="M3 3v18h18"/><path d="M7 15v3m5-8v8m5-13v13"/></>,
  user: <><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.5 3.1-7 8-7s8 2.5 8 7"/></>,
  bell: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 8-3 9h18c0-1-3-2-3-9"/><path d="M10 21h4"/></>,
  arrow: <><path d="M4 12h15m-6-6 6 6-6 6"/></>,
  back: <><path d="m14 5-7 7 7 7"/></>,
  eye: <><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></>,
  'eye-off': <><path d="M3 3 21 21M10 5.2A11 11 0 0 1 12 5c6 0 10 7 10 7a16 16 0 0 1-3 3.5M6.1 6.1C3.5 8 2 12 2 12s4 7 10 7a10 10 0 0 0 4.3-1"/></>,
  transfer: <><path d="M3 8h18l-4-4m4 4-4 4M21 16H3l4-4m-4 4 4 4"/></>,
  receipt: <><path d="M6 2h12v20l-3-2-3 2-3-2-3 2Z"/><path d="M9 7h6M9 11h6M9 15h4"/></>,
  camera: <><path d="M4 7h4l2-3h4l2 3h4a1 1 0 0 1 1 1v11H3V8a1 1 0 0 1 1-1Z"/><circle cx="12" cy="13" r="4"/></>,
  wallet: <><rect x="2" y="5" width="20" height="16" rx="3"/><path d="M17 12h5M4 5V3h15"/></>,
  shield: <><path d="m12 2 9 4v6c0 5-3 8-9 10-6-2-9-5-9-10V6Z"/><path d="m8 12 3 3 5-6"/></>,
  memory: <><path d="M12 3a5 5 0 0 0-8 4 5 5 0 0 0-1 8 5 5 0 0 0 8 5V4m1-1a5 5 0 0 1 8 4 5 5 0 0 1 1 8 5 5 0 0 1-8 5V4"/></>,
  tasks: <><path d="M9 5h12M9 12h12M9 19h12M3 5l1.5 1.5L7 4M3 12l1.5 1.5L7 11M3 19l1.5 1.5L7 18"/></>,
  cards: <><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20m-16 5h4"/></>,
  send: <><path d="m3 20 18-8L3 4l2 8-2 8Zm2-8h16"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  settings: <><circle cx="12" cy="12" r="3"/><path d="M12 2v3m0 14v3M4 4l2 2m12 12 2 2M2 12h3m14 0h3M4 20l2-2M18 6l2-2"/></>,
  logout: <><path d="M10 3H4v18h6m4-4 5-5-5-5m5 5H9"/></>,
  refresh: <><path d="M20 7V3l-3 3A9 9 0 1 0 21 12"/></>,
  check: <><path d="m4 12 5 5L20 6"/></>,
  menu: <><path d="M4 6h16M4 12h16M4 18h16"/></>,
  chevron: <><path d="m9 5 7 7-7 7"/></>,
  info: <><circle cx="12" cy="12" r="10"/><path d="M12 11v6M12 7h.01"/></>,
}
export function MobileIcon({name, size=20, ...props}: {name:MobileIconName;size?:number}&SVGProps<SVGSVGElement>){
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{paths[name]}</svg>
}
