// Theme handling (blueprint 04 §4.9/§store): dark-first, persisted, prefers-color-scheme aware.
export type Theme = 'dark' | 'light'

const KEY = 'shelfwise-theme'

export function currentTheme(): Theme {
  const saved = typeof localStorage !== 'undefined' ? (localStorage.getItem(KEY) as Theme | null) : null
  if (saved === 'dark' || saved === 'light') return saved
  // Guarded: jsdom/SSR have no matchMedia. Dark is the default per the design system.
  return typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: light)').matches
    ? 'light'
    : 'dark'
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme === 'light' ? 'light' : ''
  try {
    localStorage.setItem(KEY, theme)
  } catch {
    /* private mode — ignore */
  }
}

export function initTheme(): void {
  applyTheme(currentTheme())
}
