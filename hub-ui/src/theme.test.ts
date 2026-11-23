import { describe, expect, it } from "vitest";
import initScript from "../public/assets/theme-init.js?raw";
import { applyTheme, darkQuery, parseThemeChoice, readThemeChoice, resolveTheme, THEME_STORAGE_KEY, writeThemeChoice } from "./theme";

const blocked = () => { throw new Error("storage blocked"); };
const stored = (value: string | null) => () => ({ getItem: (key: string) => (key === THEME_STORAGE_KEY ? value : null) });
const media = (matches: boolean) => ({ matches });

describe("theme choice", () => {
  it("defaults to dark and accepts only the three choices", () => {
    expect(parseThemeChoice(null)).toBe("dark");
    expect(parseThemeChoice("solarized")).toBe("dark");
    expect(parseThemeChoice("light")).toBe("light");
    expect(parseThemeChoice("system")).toBe("system");
  });

  it("reads the stored choice", () => {
    expect(readThemeChoice(stored("light"))).toBe("light");
    expect(readThemeChoice(stored(null))).toBe("dark");
    expect(readThemeChoice(stored("nonsense"))).toBe("dark");
  });

  it("falls back to dark when storage fails", () => {
    expect(readThemeChoice(blocked)).toBe("dark");
    expect(readThemeChoice(() => ({ getItem: blocked }))).toBe("dark");
  });

  it("stores the choice and survives a failed write", () => {
    const written: string[][] = [];
    writeThemeChoice("system", () => ({ setItem: (key, value) => { written.push([key, value]); } }));
    expect(written).toEqual([["engineeringHub.theme", "system"]]);
    expect(() => writeThemeChoice("light", blocked)).not.toThrow();
    expect(() => writeThemeChoice("light", () => ({ setItem: blocked }))).not.toThrow();
  });

  it("resolves explicit choices without asking the system", () => {
    expect(resolveTheme("dark", media(false))).toBe("dark");
    expect(resolveTheme("light", media(true))).toBe("light");
  });

  it("resolves System from the operating system, or dark when it cannot tell", () => {
    expect(resolveTheme("system", media(true))).toBe("dark");
    expect(resolveTheme("system", media(false))).toBe("light");
    expect(resolveTheme("system", null)).toBe("dark");
    expect(darkQuery(blocked)).toBeNull();
  });

  it("applies the theme to the root element", () => {
    const attributes: Record<string, string> = {};
    applyTheme("light", { setAttribute: (name, value) => { attributes[name] = value; } });
    expect(attributes).toEqual({ "data-theme": "light" });
  });
});

describe("theme-init.js, the pre-paint script", () => {
  function run(localStorage: unknown, matchMedia: unknown) {
    let theme: string | undefined;
    const document = { documentElement: { setAttribute: (_name: string, value: string) => { theme = value; } } };
    new Function("localStorage", "matchMedia", "document", initScript)(localStorage, matchMedia, document);
    return theme;
  }
  const storage = (value: string | null) => ({ getItem: () => value });
  const prefers = (dark: boolean) => () => ({ matches: dark });

  it("matches src/theme.ts for every stored value", () => {
    for (const value of ["dark", "light", "system", "nonsense", null]) {
      for (const dark of [true, false]) {
        expect(run(storage(value), prefers(dark))).toBe(resolveTheme(parseThemeChoice(value), media(dark)));
      }
    }
  });

  it("falls back to dark when storage or the media query fails", () => {
    expect(run({ getItem: blocked }, prefers(false))).toBe("dark");
    expect(run(storage("system"), blocked)).toBe("dark");
  });
});
