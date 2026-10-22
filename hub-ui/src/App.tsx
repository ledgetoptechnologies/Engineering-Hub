import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { HubApiError, HubClient } from "./api";
import { TopicRefreshBudgetExhausted, watchTopicRegistry } from "./topicEvents";
import { NotesPanel } from "./NotesPanel";
import type { Hub } from "./hub";
import { href, useRoute, type Route } from "./router";
import { Badge, Icon, Modal, PageHeader, pretty, type IconKind } from "./ui";
import { HomePage } from "./pages/HomePage";
import { InboxPage, ProjectPage, ProjectsPage, WorkPage } from "./pages/WorkPages";
import { AgentsPage, StagingPage } from "./pages/SystemPages";
import { openDecisions } from "./pages/decisions";
import { LabelSetPage } from "./pages/LabelSets";
import type {
  Decision,
  EventsStatus,
  Overview,
  ProjectId,
  PublicReachability,
  Session,
  TopicSnapshot,
} from "./types";

/** Two letters from an email's local part ("bkoltz@…" -> "BK"); "?" when unknown. */
function initials(value: string | null | undefined): string {
  const letters = (value ?? "").split("@")[0].replace(/[^A-Za-z]/g, "");
  return letters ? letters.slice(0, 2).toUpperCase() : "?";
}

const topicDenied = (error: unknown) =>
  error instanceof HubApiError && error.status === 403 &&
  ["topic_read_forbidden", "ingress_forbidden"].includes(error.code);
const topicDenialText = (error: unknown) =>
  error instanceof HubApiError && error.code === "ingress_forbidden"
    ? "The selected topic request was rejected by ingress. Check the topic and retry; workspace records remain available."
    : "This session cannot read the selected topic. Choose an authorized topic; workspace records remain available.";
const errorText = (error: unknown) =>
  error instanceof HubApiError
    ? error.status === 401
      ? "Your session has ended. Sign in again."
      : error.status === 409
        ? "This record changed. Refresh before trying again."
        : error.status === 503
          ? "This service is not configured or available yet."
          : error.status === 403
            ? "This action requires your authorized session and current security token."
            : "The request could not be completed. Your changes were not confirmed."
    : "The connection could not be verified. No result is being assumed.";

export function HubApp() {
  const demo = new URLSearchParams(location.search).get("demo") === "1";
  const client = useMemo(
    () => new HubClient({ mode: demo ? "demo" : "live" }),
    [demo],
  );
  const [session, setSession] = useState<Session | null>(null);
  const [data, setData] = useState<Overview | null>(null);
  const [peers, setPeers] = useState<TopicSnapshot | null>(null);
  const [topicError, setTopicError] = useState("");
  const [topicStale, setTopicStale] = useState(false);
  const [registryEpoch, setRegistryEpoch] = useState<string | null>(null);
  const [registryRetry, setRegistryRetry] = useState(0);
  const [events, setEvents] = useState<EventsStatus | null>(null);
  const [topic, setTopic] = useState("auto-wake-v1-test");
  const [topicDraft, setTopicDraft] = useState(topic);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [notice, setNotice] = useState("");
  const [noticeError, setNoticeError] = useState(false);
  const [connection, setConnection] = useState(demo ? "demo" : "checking");
  const [stale, setStale] = useState(false);
  const [expiryTick, expire] = useState(0);
  const [newTask, setNewTask] = useState(false);
  const [taskTitle, setTaskTitle] = useState("");
  const [taskProject, setTaskProject] = useState<ProjectId>("engineering-hub");
  const [pauseChoice, setPauseChoice] = useState<boolean | null>(null);
  const [decisionChoices, setDecisionChoices] = useState<
    Record<string, { revision: number; answer: string }>
  >({});
  const [protectedChoice, setProtectedChoice] = useState<{
    id: string;
    revision: number;
    title: string;
    answer: string;
  } | null>(null);
  const requests = useRef(0);
  const topicReads = useRef(0);
  const taskRequest = useRef<{ payload: string; key: string } | null>(null);

  const clearPrivateWorkspace = useCallback(() => {
    // Also fence late reads from a request whose identity is no longer valid.
    requests.current++;
    topicReads.current++;
    setSession(null);
    setData(null);
    setPeers(null);
    setRegistryEpoch(null);
    setEvents(null);
    setTopicError("");
    setTopicStale(false);
    setLoading(false);
    setStale(true);
    setNewTask(false);
    setTaskTitle("");
    setTaskProject("engineering-hub");
    taskRequest.current = null;
    setPauseChoice(null);
    setDecisionChoices({});
    setProtectedChoice(null);
    // Clear the editable draft without changing the applied topic: changing it
    // would trigger another refresh and could immediately undo fail-closed UI.
    setTopicDraft("");
  }, []);

  const refresh = useCallback(
    async (canVerify: () => boolean = () => true) => {
      const request = ++requests.current;
      setLoading(true);
      try {
        const nextSession = await client.getSession();
        if (request !== requests.current) return false;
        setSession(nextSession);
        if (!nextSession.authenticated) {
          clearPrivateWorkspace();
          // Retain only the unauthenticated response's public provider hint;
          // private workspace/session data was cleared above.
          setSession(nextSession);
          return false;
        }
        setPeers(null);
        setTopicError("");
        const topicRead = ++topicReads.current;
        // Topic evidence is independent of workspace records. A slow or hung
        // observer must not hold overview loading or the human task controls.
        void client.getTopic(topic).then(
          (snapshot) => {
            if (request !== requests.current) return;
            if (topicRead !== topicReads.current) return;
            if (!canVerify()) return; // A disconnected SSE generation cannot revive readiness.
            setPeers(snapshot);
            setRegistryEpoch(snapshot.event_source === "canonical_registry" ? snapshot.registry_epoch ?? null : null);
            if (snapshot.event_source !== "canonical_registry") setTopicStale(false);
            setTopicError("");
          },
          (error) => {
            if (request !== requests.current) return;
            if (topicRead !== topicReads.current) return;
            setPeers(null);
            if (topicDenied(error)) {
              setRegistryEpoch(null);
              setTopicStale(true);
              setTopicError(topicDenialText(error));
              return;
            }
            if (
              error instanceof HubApiError &&
              [401, 403].includes(error.status)
            ) {
              setNotice(errorText(error));
              setNoticeError(true);
              clearPrivateWorkspace();
            } else {
              setTopicError(
                "Topic status unavailable. Check the topic and retry; workspace records remain available.",
              );
            }
          },
        );
        const [overview, status] = await Promise.all([
          client.getOverview(),
          client.getEventsStatus(),
        ]);
        if (request !== requests.current) return false;
        setData(overview);
        setEvents(status);
        setStale(!canVerify());
        if (!status.available && canVerify())
          setConnection(demo ? "demo" : "manual");
        return true;
      } catch (error) {
        if (request !== requests.current) return false;
        setNotice(errorText(error));
        setNoticeError(true);
        setStale(true);
        if (error instanceof HubApiError && [401, 403].includes(error.status)) {
          clearPrivateWorkspace();
        }
        return false;
      } finally {
        if (request === requests.current) setLoading(false);
      }
    },
    [client, topic, demo, clearPrivateWorkspace],
  );
  useEffect(() => {
    void refresh();
    return () => {
      requests.current++;
    };
  }, [refresh]);

  useEffect(() => {
    if (demo || !session?.authenticated || !events?.available) return;
    let active = true;
    let connected = false;
    let generation = 0;
    let refreshing = false;
    let dirty = false;
    const source = new EventSource("/api/hub/v1/events", {
      withCredentials: true,
    });
    source.onopen = () => {
      if (active) {
        connected = true;
        generation++;
        setConnection("reconciling");
        setStale(true);
      }
    };
    async function reconcile() {
      if (refreshing) {
        dirty = true;
        return;
      }
      refreshing = true;
      try {
        do {
          dirty = false;
          const expected = generation;
          const current = () =>
            active && connected && generation === expected && !dirty;
          const ok = await refresh(current);
          if (current() && ok) {
            setConnection("live");
            setStale(false);
          }
        } while (active && connected && dirty);
      } finally {
        refreshing = false;
      }
    }
    source.onmessage = (event) => {
      if (!active) return;
      try {
        const pointer = JSON.parse(event.data);
        if (pointer.kind === "source_unavailable") {
          connected = false;
          generation++;
          source.close();
          setConnection("manual");
          setStale(true);
          return;
        }
        if (pointer.kind !== "invalidate") return;
        setStale(true);
        void reconcile();
      } catch {
        connected = false;
        generation++;
        source.close();
        setConnection("manual");
        setStale(true);
      }
    };
    source.onerror = () => {
      if (active) {
        connected = false;
        generation++;
        setConnection("reconnecting");
        setStale(true);
      }
    };
    return () => {
      active = false;
      connected = false;
      generation++;
      source.close();
    };
  }, [demo, session?.authenticated, events?.available, refresh]);

  useEffect(() => {
    if (demo || !session?.authenticated || !registryEpoch) return;
    return watchTopicRegistry({
      topic,
      epoch: registryEpoch,
      fetchSnapshot: () => client.getTopic(topic),
      accept: (snapshot) => {
        topicReads.current++;
        setPeers(snapshot);
        setTopicStale(false);
        setTopicError("");
      },
      invalidate: () => {
        topicReads.current++;
        setTopicStale(true);
      },
      failed: (error) => {
        if (topicDenied(error)) {
          setPeers(null);
          setRegistryEpoch(null);
          setTopicError(topicDenialText(error));
        } else if (error instanceof HubApiError && [401, 403].includes(error.status)) {
          setNotice(errorText(error));
          setNoticeError(true);
          clearPrivateWorkspace();
        } else setTopicError(error instanceof TopicRefreshBudgetExhausted
          ? "Topic snapshot budget reached. Readiness is unverified; use Check to reconnect. Workspace records remain available."
          : "Topic event source unavailable. Readiness is unverified; use Check to reconnect. Workspace records remain available.");
      },
    });
  }, [client, demo, session?.authenticated, topic, registryEpoch, registryRetry, clearPrivateWorkspace]);

  useEffect(() => {
    const topicDeadlines = (peers?.items ?? [])
      .filter(
        (peer) =>
          ["reachable", "busy_queued"].includes(peer.status) &&
          peer.valid_until,
      )
      .map((peer) => Date.parse(peer.valid_until!))
      .filter((time) => time > Date.now());
    const deadlines = [...topicDeadlines, ...(data?.agents ?? [])
      .map((agent) => Math.min(Date.parse(agent.connection?.valid_until ?? ""),
        Date.parse(agent.connection?.observed_at ?? "") + 30_000))
      .filter((time) => Number.isFinite(time) && time > Date.now())];
    if (!deadlines.length) return;
    // Local expiry only: no HTTP, inbox, model, or status polling.
    const timer = setTimeout(
      () => expire((value) => value + 1),
      Math.min(...deadlines) - Date.now() + 10,
    );
    return () => clearTimeout(timer);
  }, [peers, data, expiryTick]);

  async function mutate(operation: () => Promise<unknown>, success: string) {
    setWorking(true);
    setNotice("");
    try {
      await operation();
      setNotice(demo ? `${success} Demo only; no server changed.` : success);
      setNoticeError(false);
      await refresh();
      return true;
    } catch (error) {
      setNotice(errorText(error));
      setNoticeError(true);
      if (
        error instanceof HubApiError &&
        [401, 403, 409].includes(error.status)
      ) {
        setStale(true);
        if ([401, 403].includes(error.status)) {
          clearPrivateWorkspace();
        }
      }
      return false;
    } finally {
      setWorking(false);
    }
  }
  const writable = Boolean(
    session?.authenticated && data && !loading && !working && !(stale && !demo),
  );
  useEffect(() => {
    // A selected answer is bound to the exact current decision revision.
    // Any stale workspace invalidates both intent and pending confirmation.
    setDecisionChoices((previous) => {
      if (stale || !data) return Object.keys(previous).length ? {} : previous;
      const valid = Object.fromEntries(
        Object.entries(previous).filter(([id, choice]) =>
          data.decisions.some(
            (item) =>
              item.id === id &&
              item.status === "open" &&
              item.revision === choice.revision &&
              item.options.includes(choice.answer),
          ),
        ),
      );
      return Object.keys(valid).length === Object.keys(previous).length
        ? previous
        : valid;
    });
    setProtectedChoice((previous) => {
      if (!previous) return null;
      return !stale &&
        data?.decisions.some(
          (item) =>
            item.id === previous.id &&
            item.status === "open" &&
            item.revision === previous.revision &&
            item.title === previous.title &&
            item.options.includes(previous.answer),
        )
        ? previous
        : null;
    });
  }, [stale, data]);
  const projectName = (id: string) =>
    data?.projects.find((item) => item.id === id)?.name ?? pretty(id);
  const peerState = (peer: PublicReachability) =>
    (!demo && (stale || topicStale)) ||
    (["reachable", "busy_queued"].includes(peer.status) &&
      (!peer.valid_until || !Number.isFinite(Date.parse(peer.valid_until)) || Date.parse(peer.valid_until) <= Date.now()))
      ? "unknown_stale"
      : peer.status;

  function checkTopic(event: FormEvent) {
    event.preventDefault();
    if (topicDraft !== topic) {
      topicReads.current++;
      setPeers(null);
      setRegistryEpoch(null);
      setTopicStale(false);
      setTopicError("");
      setTopic(topicDraft);
    } else {
      setRegistryRetry((value) => value + 1);
      void refresh();
    }
  }

  async function saveTask(event: FormEvent) {
    event.preventDefault();
    const payload = JSON.stringify([taskProject, taskTitle.trim()]);
    if (!taskRequest.current || taskRequest.current.payload !== payload)
      taskRequest.current = { payload, key: crypto.randomUUID() };
    const ok = await mutate(
      () =>
        client.createTask(
          taskProject,
          taskTitle.trim(),
          taskRequest.current!.key,
          session!.csrf_token!,
        ),
      "Task assigned.",
    );
    if (ok) {
      taskRequest.current = null;
      setNewTask(false);
      setTaskTitle("");
    }
  }
  async function recordDecision(
    event: FormEvent<HTMLFormElement>,
    decision: Decision,
  ) {
    event.preventDefault();
    const choice = decisionChoices[decision.id];
    if (
      !writable ||
      !choice ||
      choice.revision !== decision.revision ||
      !decision.options.includes(choice.answer)
    )
      return;
    if (decision.protected) {
      setProtectedChoice({
        id: decision.id,
        revision: decision.revision,
        title: decision.title,
        answer: choice.answer,
      });
      return;
    }
    await submitDecision(decision.id, decision.revision, choice.answer);
  }
  async function submitDecision(id: string, revision: number, answer: string) {
    if (
      !writable ||
      !data?.decisions.some(
        (item) =>
          item.id === id &&
          item.revision === revision &&
          item.status === "open" &&
          item.options.includes(answer),
      )
    )
      return false;
    return mutate(
      () => client.answerDecision(id, revision, answer, session!.csrf_token!),
      "Decision recorded, not an execution grant.",
    );
  }

  const route = useRoute();
  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => setMenuOpen(false), [route]);
  const hub: Hub | null = data ? {
    demo, session, data, stale, loading, writable, connection, projectName, decisionChoices,
    chooseAnswer: (decision: Decision, answer: string) =>
      setDecisionChoices((previous) => ({ ...previous, [decision.id]: { revision: decision.revision, answer } })),
    recordDecision: (event: FormEvent<HTMLFormElement>, decision: Decision) => void recordDecision(event, decision),
    requestPause: (paused: boolean) => setPauseChoice(paused),
    clearPrivateWorkspace, peers, topicDraft, setTopicDraft, checkTopic, topicError, topicStale, peerState,
  } : null;
  const waitingCount = hub ? openDecisions(hub).length : 0;
  const activeWork = data ? data.tasks.filter((task) => task.status !== "verified").length : 0;
  const nav: { route: Route; label: string; icon: IconKind; count?: number; attention?: boolean }[] = [
    { route: { page: "home" }, label: "Home", icon: "grid" },
    { route: { page: "inbox" }, label: "Needs you", icon: "inbox", count: data ? waitingCount : undefined, attention: waitingCount > 0 },
    { route: { page: "work" }, label: "Work", icon: "work", count: data ? activeWork : undefined },
    { route: { page: "projects" }, label: "Projects", icon: "folder" },
    { route: { page: "notes" }, label: "Notes", icon: "notes" },
    { route: { page: "agents" }, label: "Agents", icon: "agents" },
    { route: { page: "staging" }, label: "Staging", icon: "staging" },
  ];
  const isCurrent = (target: Route) => target.page === route.page
    || (target.page === "projects" && (route.page === "project" || route.page === "labels"));
  const pageTitle = route.page === "project" ? projectName(route.id)
    : route.page === "labels" ? `${projectName(route.project)} labeling`
    : nav.find((item) => item.route.page === route.page)?.label ?? "Home";
  const actions = (
    <>
      <button className="secondary" onClick={() => void refresh()} disabled={loading} aria-label="Refresh workspace">
        <Icon kind="refresh" />
        <span>{loading ? "Refreshing" : "Refresh"}</span>
      </button>
      <button className="primary" disabled={!writable} onClick={() => setNewTask(true)}>
        <Icon kind="plus" />
        New task
      </button>
    </>
  );
  const notesProps = {
    mode: (demo ? "demo" : "live") as "demo" | "live",
    knownProjects: data?.projects.map((item) => ({ id: item.id, name: item.name })) ?? [],
    csrf: session?.csrf_token ?? null, writable: !!session?.authenticated,
    viewerActor: session?.actor ?? null, onUnauthorized: clearPrivateWorkspace,
  };

  return (
    <div className={`shell ${menuOpen ? "menu-open" : ""}`}>
      <aside className="sidebar">
        <a className="brand" href={demo ? "/?demo=1" : "/"}>
          <span className="brand-mark">H</span>
          <span>
            Engineering
            <br />
            <strong>Hub</strong>
          </span>
        </a>
        <button className="menu-toggle" aria-label={menuOpen ? "Close navigation" : "Open navigation"}
          aria-expanded={menuOpen} onClick={() => setMenuOpen((value) => !value)}>
          <Icon kind="menu" />
        </button>
        <div className="sidebar-scroll">
          <div className="workspace-label">
            WORKSPACE <span>PRIVATE</span>
          </div>
          <nav aria-label="Workspace navigation">
            {nav.map((item) => (
              <a key={item.label} className={isCurrent(item.route) ? "nav-item selected" : "nav-item"}
                href={href(item.route)} aria-current={isCurrent(item.route) ? "page" : undefined}>
                <Icon kind={item.icon} /> {item.label}
                {item.count !== undefined && item.count > 0 && (
                  <span className={`nav-count ${item.attention ? "attention" : ""}`}>{item.count}</span>
                )}
              </a>
            ))}
          </nav>
          <div className="sidebar-section">
            PROJECTS <span>{data?.projects.length ?? "—"}</span>
          </div>
          <nav aria-label="Projects" className="project-nav">
            {data?.projects.map((item) => (
              <a key={item.id} href={`#/projects/${item.id}`}
                className={(route.page === "project" && route.id === item.id) || (route.page === "labels" && route.project === item.id)
                  ? "project-link current" : "project-link"}>
                <span className="project-square" />
                {item.name}
              </a>
            )) ?? <span className="sidebar-help">Sign in to load projects.</span>}
          </nav>
        </div>
        <div className="sidebar-bottom">
          <div className="environment-note">
            <span className="dot" /> Staging, not production
            <p>Processing workloads stay separate.</p>
          </div>
          <div className="profile" title={session?.authenticated
            ? session.email ?? `${session.actor ?? "Authorized operator"}` : "Sign-in required"}>
            <span className="avatar" aria-hidden="true">
              {demo ? "D" : session?.authenticated ? initials(session.email ?? session.actor) : "—"}
            </span>
            <div>
              <strong>{demo ? "Demo workspace" : session?.authenticated ? session.email ?? session.actor : "Private workspace"}</strong>
              <small>
                {demo ? "Sample data only" : session?.authenticated
                  ? session.auth_provider === "cloudflare_access" ? "Cloudflare Access" : `GitHub ID ${session.github_id ?? "unknown"}`
                  : "Sign-in required"}
              </small>
            </div>
          </div>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div className="breadcrumb">
            Workspace <span>/</span> {data ? pageTitle : "Overview"}
          </div>
          <div className="topbar-right">
            <span className={`feed-pill ${stale && !demo ? "stale" : connection === "live" ? "live" : ""}`}>
              {demo ? "Sample data" : stale ? "Snapshot unverified" : connection === "live" ? "Event feed connected" : "Manual refresh"}
            </span>
            <span className="date">
              {new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "America/Chicago" }).format(new Date())}
            </span>
            <Badge tone={demo ? "amber" : "forest"}>{demo ? "DEMO" : "PRIVATE"}</Badge>
            {session?.authenticated && !demo && (
              <button className="text-button" onClick={() => void mutate(() => client.logout(session.csrf_token!), "Signed out.")}>
                Sign out
              </button>
            )}
          </div>
        </header>
        {demo && (
          <div className="demo-banner">
            <span>
              <strong>Demo workspace.</strong> Sample records and local-only actions. Not your live infrastructure.
            </span>
            <a href="/">Open live view <span>↗</span></a>
          </div>
        )}
        {notice && (
          <div role={noticeError ? "alert" : "status"} className={`notice ${noticeError ? "error" : "success"}`}>
            {notice}
          </div>
        )}
        {!hub ? (
          <>
            <PageHeader eyebrow="DEVELOPMENT / STAGING" title="Engineering, in view"
              subtitle="Your projects, your agents, and a clear next step.">{actions}</PageHeader>
            <section className="login-panel">
              <span className="login-symbol">H</span>
              <p className="eyebrow">YOUR PRIVATE CONTROL PLANE</p>
              <h2>{loading ? "Opening your workspace…" : "Sign in to Engineering Hub."}</h2>
              <p>
                Use your authorized account to see assigned work and record decisions. Agent credentials never
                belong in this browser.
              </p>
              {!loading && (
                <>
                  <a className="primary" href="/api/hub/v1/auth/login">Sign in securely <Icon kind="arrow" /></a>
                  <p className="small-text">
                    {session?.auth_provider === "cloudflare_access"
                      ? "Cloudflare Access protects this workspace. If you signed out, continue to open a new Hub session."
                      : "Sign-in is available only after the approved identity provider is configured. Agent tokens cannot be used here."}
                  </p>
                  <a className="text-link" href="/?demo=1">Explore a clearly labelled demo instead →</a>
                </>
              )}
            </section>
          </>
        ) : (
          <div className="page" key={route.page === "project" ? `project-${route.id}`
            : route.page === "labels" ? `labels-${route.project}-${route.set}` : route.page}>
            {route.page === "home" && <HomePage hub={hub} actions={actions} />}
            {route.page === "inbox" && <InboxPage hub={hub} actions={actions} />}
            {route.page === "work" && <WorkPage hub={hub} actions={actions} />}
            {route.page === "projects" && <ProjectsPage hub={hub} actions={actions} />}
            {route.page === "project" && <ProjectPage hub={hub} id={route.id} actions={actions} notesProps={notesProps} />}
            {route.page === "notes" && (
              <>
                <PageHeader eyebrow="NOTES" title="Shared notes"
                  subtitle="Durable notes shared with the agents. Context only, never approvals.">{actions}</PageHeader>
                <NotesPanel {...notesProps} selectedProject="all" />
              </>
            )}
            {route.page === "labels" && <LabelSetPage hub={hub} project={route.project} setId={route.set} actions={actions} />}
            {route.page === "agents" && <AgentsPage hub={hub} actions={actions} />}
            {route.page === "staging" && <StagingPage hub={hub} actions={actions} />}
          </div>
        )}
        <footer className="page-footer">
          <span>
            ENGINEERING HUB <span className="footer-separator">/</span> PRIVATE CONTROL PLANE
          </span>
          <span>Code, staging & production stay separate.</span>
        </footer>
      </main>
      {protectedChoice && (
        <Modal title="Confirm protected decision" onClose={() => setProtectedChoice(null)}>
          <p className="modal-copy">This records your decision only. It does not grant permission to execute or deploy.</p>
          <h3>{protectedChoice.title}</h3>
          <p className="modal-copy">Selected answer: <strong>{protectedChoice.answer}</strong></p>
          <div className="modal-actions">
            <button className="secondary" onClick={() => setProtectedChoice(null)}>Cancel</button>
            <button className="primary" disabled={!writable} onClick={async () => {
              const ok = await submitDecision(protectedChoice.id, protectedChoice.revision, protectedChoice.answer);
              if (ok) setProtectedChoice(null);
            }}>
              Confirm decision
            </button>
          </div>
        </Modal>
      )}
      {newTask && (
        <Modal title="Assign a task" onClose={() => setNewTask(false)}>
          <form onSubmit={(event) => void saveTask(event)}>
            <p className="modal-copy">
              Define the goal. An agent can claim it and request independent review. This does not activate a deployment.
            </p>
            <label className="field">
              Project
              <select value={taskProject} onChange={(event) => setTaskProject(event.target.value as ProjectId)}>
                {data?.projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
              </select>
            </label>
            <label className="field">
              What needs to happen?
              <textarea autoFocus value={taskTitle} onChange={(event) => setTaskTitle(event.target.value)}
                maxLength={200} required rows={3} placeholder="A concrete, testable outcome…" />
            </label>
            <div className="modal-actions">
              <button className="secondary" type="button" onClick={() => setNewTask(false)}>Cancel</button>
              <button className="primary" disabled={!writable || !taskTitle.trim()}>Assign task</button>
            </div>
          </form>
        </Modal>
      )}
      {pauseChoice !== null && (
        <Modal title={pauseChoice ? "Pause new staging work?" : "Resume new staging work?"} onClose={() => setPauseChoice(null)}>
          <p className="modal-copy">
            This changes admission of new staging work. Active actions reach a safe boundary; the MCP and production
            are not stopped. It does not enable an unconfigured executor.
          </p>
          <div className="modal-actions">
            <button className="secondary" onClick={() => setPauseChoice(null)}>Cancel</button>
            <button className="primary" disabled={!writable} onClick={async () => {
              const ok = await mutate(() => client.setPause(pauseChoice, session!.csrf_token!),
                pauseChoice ? "New staging work paused." : "New staging work resumed.");
              if (ok) setPauseChoice(null);
            }}>
              {pauseChoice ? "Pause new work" : "Resume new work"}
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
