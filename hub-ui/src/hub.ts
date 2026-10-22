/** The view of Hub state that pages read. All mutation still goes through HubApp. */
import type { FormEvent } from "react";
import type { Decision, Overview, PublicReachability, Session, TopicSnapshot } from "./types";

export interface DecisionChoice { revision: number; answer: string }

export interface Hub {
  demo: boolean;
  session: Session | null;
  data: Overview;
  stale: boolean;
  loading: boolean;
  writable: boolean;
  connection: string;
  projectName: (id: string) => string;
  decisionChoices: Record<string, DecisionChoice>;
  chooseAnswer: (decision: Decision, answer: string) => void;
  recordDecision: (event: FormEvent<HTMLFormElement>, decision: Decision) => void;
  requestPause: (paused: boolean) => void;
  clearPrivateWorkspace: () => void;
  /** Re-read the workspace snapshot (after a successful human change). */
  refresh: () => Promise<void>;
  // Topic readiness (Agents page).
  peers: TopicSnapshot | null;
  topicDraft: string;
  setTopicDraft: (value: string) => void;
  checkTopic: (event: FormEvent) => void;
  /** The topic whose readiness is currently shown. */
  topic: string;
  selectTopic: (topic: string) => void;
  topicError: string;
  topicStale: boolean;
  peerState: (peer: PublicReachability) => string;
}
