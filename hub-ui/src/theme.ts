/**
 * Theme choice for this device: Dark (default), Light or System. System follows the operating system.
 * public/assets/theme-init.js applies the same rules before first paint; keep the two in step.
 * Storage or matchMedia can throw (blocked site data, private windows), and every failure means dark.
 */
export type Theme = "dark" | "light";
export type ThemeChoice = Theme | "system";

export const THEME_STORAGE_KEY = "engineeringHub.theme";
export const DEFAULT_THEME_CHOICE: ThemeChoice = "dark";
export const THEME_CHOICES: { value: ThemeChoice; label: string }[] = [
  { value: "dark", label: "Dark" },
  { value: "light", label: "Light" },
  { value: "system", label: "System" },
];

const DARK_QUERY = "(prefers-color-scheme: dark)";

export function parseThemeChoice(value: unknown): ThemeChoice {
  return THEME_CHOICES.find((choice) => choice.value === value)?.value ?? DEFAULT_THEME_CHOICE;
}

export function readThemeChoice(storage: () => Pick<Storage, "getItem"> = () => localStorage): ThemeChoice {
  try {
    return parseThemeChoice(storage().getItem(THEME_STORAGE_KEY));
  } catch {
    return DEFAULT_THEME_CHOICE;
  }
}

export function writeThemeChoice(choice: ThemeChoice, storage: () => Pick<Storage, "setItem"> = () => localStorage) {
  try { storage().setItem(THEME_STORAGE_KEY, choice); } catch { /* storage unavailable: the choice lasts until reload */ }
}

export function darkQuery(media: (query: string) => MediaQueryList = (query) => matchMedia(query)): MediaQueryList | null {
  try {
    return media(DARK_QUERY);
  } catch {
    return null;
  }
}

export function resolveTheme(choice: ThemeChoice, query: Pick<MediaQueryList, "matches"> | null): Theme {
  if (choice === "system") return query && !query.matches ? "light" : "dark";
  return choice;
}

export function applyTheme(theme: Theme, root: Pick<HTMLElement, "setAttribute"> = document.documentElement) {
  root.setAttribute("data-theme", theme);
}
