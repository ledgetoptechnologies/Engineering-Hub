/**
 * Who did something, in words. A record's actor is a legacy agent family ("codex"), a paired
 * installation ("installation:<uuid>", with its family carried separately), or a person ("human:…").
 * An installation is never shown as its bare family: another computer running the same agent
 * didn't do it.
 */
import type { Agent } from "./types";
import { names } from "./ui";

const INSTALLATION = /^installation:([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const FAMILIES: readonly string[] = ["codex", "claude", "hermes"];
export const isFamily = (value: unknown): value is Agent => typeof value === "string" && FAMILIES.includes(value);
export const isInstallationActor = (value: unknown): value is string => typeof value === "string" && INSTALLATION.test(value);

export interface ActorContext {
  /** The family recorded alongside an installation actor (owner_family, proposed_family…). */
  family?: string | null;
  /** The signed-in person, so their own actions read "You". */
  viewer?: string | null;
  /** Installation names the owner can see, by installation id (without the "installation:" prefix). */
  installations?: Record<string, string>;
}

/** The agent family behind an actor, or null for people and installations with no recorded family. */
export function actorFamily(actor: string | null | undefined, family?: string | null): Agent | null {
  if (isFamily(actor)) return actor;
  if (isInstallationActor(actor) && isFamily(family)) return family;
  return null;
}

export function actorName(actor: string, context: ActorContext = {}): string {
  if (context.viewer && actor === context.viewer) return "You";
  if (isFamily(actor)) return names[actor];
  const installation = INSTALLATION.exec(actor);
  if (installation) {
    const id = actor.slice("installation:".length);
    const family = isFamily(context.family) ? names[context.family] : null;
    const known = context.installations?.[id];
    if (known) return family ? `${family} · ${known}` : known;
    return family ? `${family} on install ${installation[1]}` : `Install ${installation[1]}`;
  }
  if (actor.startsWith("human:")) return "A person";
  return actor;
}

/** Avatar class and initial: the family's colour and letter, or a neutral "?" when unknown. */
export function actorAvatar(actor: string | null | undefined, family?: string | null): { className: string; letter: string } {
  const known = actorFamily(actor, family);
  return known ? { className: known, letter: names[known].charAt(0) } : { className: "", letter: actor ? "?" : "–" };
}
