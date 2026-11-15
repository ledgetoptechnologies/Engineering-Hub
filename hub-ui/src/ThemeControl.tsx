/** Theme picker for the top bar, which stays on screen at phone width (the sidebar footer does not). */
import { useEffect, useState } from "react";
import { applyTheme, darkQuery, parseThemeChoice, readThemeChoice, resolveTheme, THEME_CHOICES, writeThemeChoice } from "./theme";
import { Icon } from "./ui";

export function ThemeControl() {
  const [choice, setChoice] = useState(() => readThemeChoice());
  useEffect(() => {
    const query = darkQuery();
    const sync = () => applyTheme(resolveTheme(choice, query));
    sync();
    if (choice !== "system" || !query) return;
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, [choice]);
  return (
    <label className="theme-select">
      <Icon kind="theme" size={14} />
      <span className="sr-only">Theme</span>
      <select value={choice} onChange={(event) => {
        const next = parseThemeChoice(event.target.value);
        setChoice(next);
        writeThemeChoice(next);
      }}>
        {THEME_CHOICES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
      </select>
    </label>
  );
}
