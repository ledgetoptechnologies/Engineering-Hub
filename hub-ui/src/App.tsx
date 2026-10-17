import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { HubApiError, HubClient } from "./api";
import type {
  Decision,
  EventsStatus,
  Overview,
  ProjectId,
  PublicReachability,
  Session,
  TopicSnapshot,
} from "./types";

const names: Record<string, string> = {
  codex: "Codex",
  hermes: "Hermes",
  claude: "Claude",
};
const pretty = (value: string) =>
  value.replaceAll("_", " ").replace(/^\w/, (c) => c.toUpperCase());
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

function Icon({
  kind,
}: {
  kind: "grid" | "folder" | "agents" | "arrow" | "plus" | "refresh";
}) {
  const paths = {
    grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
    folder: "M3 7V4h6l3 3h9v13H3z",
    agents:
      "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M2 21v-3a5 5 0 0 1 10 0v3 M17 5a3 3 0 0 1 0 6 M16 15a5 5 0 0 1 6 5",
    arrow: "M5 12h14 M13 6l6 6-6 6",
    plus: "M12 4v16 M4 12h16",
    refresh: "M20 9a8 8 0 1 0 0 6 M20 3v6h-6",
  };
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[kind]} />
    </svg>
  );
}

function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return (
    <span className={`badge ${tone}`}>
      <span className="dot" />
      {children}
    </span>
  );
}
function PanelTitle({
  number,
  title,
  count,
  children,
}: {
  number: string;
  title: string;
  count?: number;
  children?: ReactNode;
}) {
  return (
    <div className="panel-title">
      <span className="panel-number">{number}</span>
      <h2>{title}</h2>
      {count !== undefined && <span className="count">{count}</span>}
      <span className="panel-action">{children}</span>
    </div>
  );
}
function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const headingId = useId();
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      aria-labelledby={headingId}
      onClose={onClose}
      onCancel={onClose}
    >
      <div className="modal-head">
        <h2 id={headingId}>{title}</h2>
        <button
          className="icon-button"
          aria-label="Close dialog"
          onClick={() => ref.current?.close()}
        >
          ×
        </button>
      </div>
      {children}
    </dialog>
  );
}

export function HubApp() {
  const demo = new URLSearchParams(location.search).get("demo") === "1";
  const client = useMemo(
    () => new HubClient({ mode: demo ? "demo" : "live" }),
    [demo],
  );
  const [session, setSession] = useState<Session | null>(null);
  const [data, setData] = useState<Overview | null>(null);
  const [peers, setPeers] = useState<TopicSnapshot | null>(null);
  const [events, setEvents] = useState<EventsStatus | null>(null);
  const [topic, setTopic] = useState("auto-wake-v1-test");
  const [topicDraft, setTopicDraft] = useState(topic);
  const [project, setProject] = useState<string>("all");
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
  const requests = useRef(0);
  const taskRequest = useRef<{ payload: string; key: string } | null>(null);

  const refresh = useCallback(
    async (canVerify: () => boolean = () => true) => {
      const request = ++requests.current;
      setLoading(true);
      try {
        const nextSession = await client.getSession();
        if (request !== requests.current) return false;
        setSession(nextSession);
        if (!nextSession.authenticated) {
          setData(null);
          setPeers(null);
          return false;
        }
        const [overview, snapshot, status] = await Promise.all([
          client.getOverview(),
          client.getTopic(topic),
          client.getEventsStatus(),
        ]);
        if (request !== requests.current) return false;
        setData(overview);
        setPeers(snapshot);
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
        if (error instanceof HubApiError && error.status === 401) {
          setSession(null);
          setData(null);
          setPeers(null);
        }
        return false;
      } finally {
        if (request === requests.current) setLoading(false);
      }
    },
    [client, topic, demo],
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
    const deadlines = (peers?.items ?? [])
      .filter(
        (peer) =>
          ["reachable", "busy_queued"].includes(peer.status) &&
          peer.valid_until,
      )
      .map((peer) => Date.parse(peer.valid_until!))
      .filter((time) => time > Date.now());
    if (!deadlines.length) return;
    // Local expiry only: no HTTP, inbox, model, or status polling.
    const timer = setTimeout(
      () => expire((value) => value + 1),
      Math.min(...deadlines) - Date.now() + 10,
    );
    return () => clearTimeout(timer);
  }, [peers, expiryTick]);

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
        if (error.status === 401) {
          requests.current++;
          setSession(null);
          setData(null);
          setPeers(null);
          setNewTask(false);
          setPauseChoice(null);
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
  const openDecisions =
    data?.decisions.filter(
      (item) =>
        item.status === "open" &&
        (project === "all" || item.project === project),
    ) ?? [];
  const tasks =
    data?.tasks.filter(
      (item) => project === "all" || item.project === project,
    ) ?? [];
  const projects =
    data?.projects.filter((item) => project === "all" || item.id === project) ??
    [];
  const projectName = (id: string) =>
    data?.projects.find((item) => item.id === id)?.name ?? pretty(id);
  const peerState = (peer: PublicReachability) =>
    (!demo && stale) ||
    (["reachable", "busy_queued"].includes(peer.status) &&
      (!peer.valid_until || Date.parse(peer.valid_until) <= Date.now()))
      ? "unknown_stale"
      : peer.status;

  function checkTopic(event: React.FormEvent) {
    event.preventDefault();
    if (topicDraft !== topic) {
      setPeers(null);
      setStale(true);
      setTopic(topicDraft);
    } else void refresh();
  }

  async function saveTask(event: React.FormEvent) {
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
    event: React.FormEvent<HTMLFormElement>,
    decision: Decision,
  ) {
    event.preventDefault();
    const answer = new FormData(event.currentTarget).get("answer") as string;
    await mutate(
      () =>
        client.answerDecision(
          decision.id,
          decision.revision,
          answer,
          session!.csrf_token!,
        ),
      "Decision recorded, not an execution grant.",
    );
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <a className="brand" href={demo ? "/?demo=1" : "/"}>
          <span className="brand-mark">H</span>
          <span>
            Engineering
            <br />
            <strong>Hub</strong>
          </span>
        </a>
        <div className="workspace-label">
          WORKSPACE <span>PRIVATE</span>
        </div>
        <nav aria-label="Workspace navigation">
          <button
            className={project === "all" ? "nav-item selected" : "nav-item"}
            onClick={() => setProject("all")}
          >
            <Icon kind="grid" /> Overview <span className="nav-arrow">↗</span>
          </button>
          <a className="nav-item" href="#work">
            <Icon kind="folder" /> Assigned work
          </a>
          <a className="nav-item" href="#agents">
            <Icon kind="agents" /> Agent connections
          </a>
        </nav>
        <div className="sidebar-section">
          PROJECTS <span>{data?.projects.length ?? "—"}</span>
        </div>
        <nav aria-label="Project filters" className="project-nav">
          {data?.projects.map((item) => (
            <button
              key={item.id}
              className={
                project === item.id ? "project-link current" : "project-link"
              }
              onClick={() => setProject(item.id)}
            >
              <span className="project-square" />
              {item.name}
            </button>
          )) ?? <span className="sidebar-help">Sign in to load projects.</span>}
        </nav>
        <div className="sidebar-bottom">
          <div className="environment-note">
            <span className="dot" /> Staging, not production
            <p>Processing workloads stay separate.</p>
          </div>
          <div className="profile">
            <span className="avatar">{demo ? "D" : "BK"}</span>
            <div>
              <strong>
                {demo
                  ? "Demo workspace"
                  : session?.authenticated
                    ? "Beau"
                    : "Private workspace"}
              </strong>
              <small>
                {demo
                  ? "Sample data only"
                  : session?.authenticated
                    ? "Authorized operator"
                    : "Sign-in required"}
              </small>
            </div>
          </div>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div className="breadcrumb">
            Workspace <span>/</span> Overview
          </div>
          <div className="topbar-right">
            <span className="date">
              {new Intl.DateTimeFormat("en-US", {
                month: "short",
                day: "numeric",
                year: "numeric",
                timeZone: "America/Chicago",
              }).format(new Date())}
            </span>
            <Badge tone={demo ? "amber" : "forest"}>
              {demo ? "DEMO" : "PRIVATE"}
            </Badge>
            {session?.authenticated && !demo && (
              <button
                className="text-button"
                onClick={() =>
                  void mutate(
                    () => client.logout(session.csrf_token!),
                    "Signed out.",
                  )
                }
              >
                Sign out
              </button>
            )}
          </div>
        </header>
        {demo && (
          <div className="demo-banner">
            <span>
              <strong>Demo workspace.</strong> Sample records and local-only
              actions. Not your live infrastructure.
            </span>
            <a href="/">
              Open live view <span>↗</span>
            </a>
          </div>
        )}
        <section className="page-heading">
          <div>
            <p className="eyebrow">DEVELOPMENT / STAGING</p>
            <h1>
              Engineering, in view<span>.</span>
            </h1>
            <p className="subtitle">
              Your projects, your agents, and a clear next step.
            </p>
          </div>
          <div className="heading-actions">
            <button
              className="secondary"
              onClick={() => void refresh()}
              disabled={loading}
              aria-label="Refresh workspace"
            >
              <Icon kind="refresh" />
              <span>{loading ? "Refreshing" : "Refresh"}</span>
            </button>
            <button
              className="primary"
              disabled={!writable}
              onClick={() => setNewTask(true)}
            >
              <Icon kind="plus" />
              New task
            </button>
          </div>
        </section>
        {notice && (
          <div
            role={noticeError ? "alert" : "status"}
            className={`notice ${noticeError ? "error" : "success"}`}
          >
            {notice}
          </div>
        )}
        {!data ? (
          <section className="login-panel">
            <span className="login-symbol">H</span>
            <p className="eyebrow">YOUR PRIVATE CONTROL PLANE</p>
            <h2>
              {loading
                ? "Opening your workspace…"
                : "Sign in to Engineering Hub."}
            </h2>
            <p>
              Use your authorized GitHub account to see assigned work and record
              decisions. Agent credentials never belong in this browser.
            </p>
            {!loading && (
              <>
                <a className="primary" href="/api/hub/v1/auth/login">
                  Continue with GitHub <Icon kind="arrow" />
                </a>
                <p className="small-text">
                  Dedicated OAuth setup is deferred. Sign-in remains unavailable
                  until it is configured.
                </p>
                <a className="text-link" href="/?demo=1">
                  Explore a clearly labelled demo instead →
                </a>
              </>
            )}
          </section>
        ) : (
          <>
            <div className="summary-strip">
              <div>
                <span className="stat-label">NEEDS YOUR INPUT</span>
                <strong>
                  {openDecisions.length.toString().padStart(2, "0")}
                  <span className="stat-note">decisions</span>
                </strong>
              </div>
              <div>
                <span className="stat-label">ASSIGNED WORK</span>
                <strong>
                  {tasks.length.toString().padStart(2, "0")}
                  <span className="stat-note">tracked tasks</span>
                </strong>
              </div>
              <div>
                <span className="stat-label">ACTIVE STAGING TARGETS</span>
                <strong>
                  {projects
                    .filter((item) => item.executor_enabled)
                    .length.toString()
                    .padStart(2, "0")}
                  <span className="stat-note">individually gated</span>
                </strong>
              </div>
              <div className="feed-summary">
                <Badge
                  tone={
                    stale
                      ? "amber"
                      : connection === "live"
                        ? "forest"
                        : "neutral"
                  }
                >
                  {demo
                    ? "Sample data"
                    : stale
                      ? "Snapshot unverified"
                      : connection === "live"
                        ? "Event feed connected"
                        : "Manual refresh"}
                </Badge>
                <p>
                  {demo
                    ? "No network writes"
                    : stale
                      ? "Refresh before making decisions"
                      : "No scheduled status polling"}
                </p>
              </div>
            </div>
            <div className="dashboard-grid">
              <section className="panel decisions" id="decisions">
                <PanelTitle
                  number="01"
                  title="Needs Beau"
                  count={openDecisions.length}
                />
                <p className="panel-description">
                  The questions that need a human, kept in one place.
                </p>
                {openDecisions.length === 0 ? (
                  <div className="empty-state">
                    <span>✓</span>
                    <h3>Nothing waiting on you.</h3>
                    <p>New decision requests will appear here.</p>
                  </div>
                ) : (
                  openDecisions.map((decision) => (
                    <form
                      className="decision"
                      key={decision.id}
                      onSubmit={(event) => void recordDecision(event, decision)}
                    >
                      <div className="item-overline">
                        {projectName(decision.project)}
                        <span>REV {decision.revision}</span>
                      </div>
                      <h3>{decision.title}</h3>
                      <fieldset disabled={!writable}>
                        <legend className="sr-only">
                          Answer for {decision.title}
                        </legend>
                        {decision.options.map((option, index) => (
                          <label className="radio-option" key={option}>
                            <input
                              type="radio"
                              name="answer"
                              value={option}
                              required
                              defaultChecked={index === 0}
                            />
                            <span>{option}</span>
                          </label>
                        ))}
                        <div className="decision-footer">
                          <span>
                            {decision.protected
                              ? "Protected request · not an execution grant"
                              : "Records a decision. Does not deploy."}
                          </span>
                          <button type="submit" className="dark-button">
                            Record decision <Icon kind="arrow" />
                          </button>
                        </div>
                      </fieldset>
                    </form>
                  ))
                )}
                <p className="panel-footnote">
                  Only your signed-in session can answer. Agents can request,
                  not approve.
                </p>
              </section>
              <section className="panel" id="staging">
                <PanelTitle number="02" title="Staging overview">
                  <Badge tone="neutral">SOFTWARE ONLY</Badge>
                </PanelTitle>
                <p className="panel-description">
                  Independent projects. Explicit activation. No production
                  shortcuts.
                </p>
                <div className="project-list">
                  {projects.map((item) => (
                    <div className="project-row" key={item.id}>
                      <span className="project-icon">
                        <Icon kind="folder" />
                      </span>
                      <div>
                        <strong>{item.name}</strong>
                        <small>
                          {item.executor_enabled
                            ? "Staging target enabled"
                            : "Awaiting inventory & acceptance"}
                        </small>
                      </div>
                      <Badge
                        tone={item.health === "healthy" ? "forest" : "neutral"}
                      >
                        {pretty(item.health)}
                      </Badge>
                    </div>
                  ))}
                </div>
                <div className="staging-control">
                  <div>
                    <span
                      className={`status-orb ${data.paused ? "paused" : ""}`}
                    />
                    <span>
                      <strong>
                        {data.paused
                          ? "New staging work paused"
                          : "New staging work not paused"}
                      </strong>
                      <small>Autonomy: {pretty(data.autonomy.state)}</small>
                    </span>
                  </div>
                  <button
                    className="secondary small"
                    disabled={!writable}
                    onClick={() => setPauseChoice(!data.paused)}
                  >
                    {data.paused ? "Resume" : "Pause new work"}
                  </button>
                </div>
                {data.runs.length > 0 && (
                  <div className="run-list">
                    <h3>Execution evidence</h3>
                    {data.runs.map((run) => (
                      <details key={run.id}>
                        <summary>
                          {projectName(run.project)}{" "}
                          <Badge
                            tone={
                              run.status === "held" || run.status === "failed"
                                ? "amber"
                                : "neutral"
                            }
                          >
                            {pretty(run.status)}
                          </Badge>
                        </summary>
                        <p className="small-text">Target: {run.target}</p>
                        <code>{run.artifact_digest}</code>
                        <p className="small-text">
                          Fence {run.fencing_generation} · {run.events.length}{" "}
                          recorded events
                        </p>
                      </details>
                    ))}
                  </div>
                )}
                <p className="panel-footnote">
                  Production, GitHub merge and real processing capabilities are
                  not enabled.
                </p>
              </section>
              <section className="panel work" id="work">
                <PanelTitle
                  number="03"
                  title="Work in motion"
                  count={tasks.length}
                />
                <p className="panel-description">
                  An owner, an exact candidate, and a separate reviewer.
                </p>
                <div className="task-table">
                  <div className="table-header">
                    <span>TASK / PROJECT</span>
                    <span>OWNER</span>
                    <span>STATE</span>
                  </div>
                  {tasks.length === 0 && (
                    <div className="empty-inline">
                      No assigned work for this project.
                    </div>
                  )}
                  {tasks.map((task) => (
                    <details className="task-row" key={task.id}>
                      <summary>
                        <span>
                          <strong>{task.title}</strong>
                          <small>{projectName(task.project)}</small>
                        </span>
                        <span className="owner">
                          <span className="mini-avatar">
                            {task.owner?.charAt(0).toUpperCase() ?? "–"}
                          </span>
                          {task.owner ? names[task.owner] : "Unclaimed"}
                        </span>
                        <Badge
                          tone={
                            task.status === "verified"
                              ? "forest"
                              : task.status === "in_review"
                                ? "amber"
                                : "neutral"
                          }
                        >
                          {task.status === "verified"
                            ? "Review passed"
                            : pretty(task.status)}
                        </Badge>
                      </summary>
                      <div className="task-detail">
                        <p>
                          Candidate:{" "}
                          <code>
                            {task.candidate_commit ?? "Not submitted"}
                          </code>
                        </p>
                        <p>
                          Independent reviewer:{" "}
                          <strong>
                            {task.review ? names[task.review.actor] : "Pending"}
                          </strong>{" "}
                          · Repairs used: {task.repair_count}/3
                        </p>
                        <p>
                          Review is bound to the exact commit, not permission to
                          deploy to production.
                        </p>
                      </div>
                    </details>
                  ))}
                </div>
              </section>
              <section className="panel" id="agents">
                <PanelTitle number="04" title="Agents & topic" />
                <p className="panel-description">
                  Connected to this conversation, not just connected somewhere.
                </p>
                <form className="topic-form" onSubmit={checkTopic}>
                  <label htmlFor="topic-root">TOPIC ROOT</label>
                  <div>
                    <input
                      id="topic-root"
                      value={topicDraft}
                      onChange={(event) => setTopicDraft(event.target.value)}
                      pattern="[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
                      maxLength={128}
                      required
                      spellCheck={false}
                    />
                    <button className="secondary small" disabled={loading}>
                      Check
                    </button>
                  </div>
                </form>
                <div className="peer-list">
                  {peers?.items.map((peer) => {
                    const state = peerState(peer);
                    return (
                      <div className="peer-row" key={peer.agent}>
                        <span className={`agent-avatar ${peer.agent}`}>
                          {names[peer.agent].charAt(0)}
                        </span>
                        <div>
                          <strong>{names[peer.agent]}</strong>
                          <small>
                            {state === "unknown_stale"
                              ? "No current, verified route"
                              : state === "busy_queued"
                                ? "Bound session can queue work"
                                : state === "reachable"
                                  ? "Exact-topic route verified"
                                  : pretty(state)}
                          </small>
                        </div>
                        <Badge
                          tone={
                            state === "reachable"
                              ? "forest"
                              : state === "busy_queued"
                                ? "amber"
                                : "neutral"
                          }
                        >
                          {state === "unknown_stale"
                            ? "Unknown"
                            : state === "linked_unverified"
                              ? "Unverified"
                              : state === "busy_queued"
                                ? "Queued"
                                : pretty(state)}
                        </Badge>
                      </div>
                    );
                  })}
                </div>
                <div className="advisory">
                  <span>↳</span>
                  <p>
                    Reachability is advisory. Accepted mail stays durable when a
                    peer is offline; a connection is not a delivery or
                    completion receipt.
                  </p>
                </div>
                <p className="panel-footnote">
                  {demo
                    ? "All agent states above are illustrative fixtures."
                    : "Live registry and owning-session evidence are required. The Hub event feed alone does not prove topic readiness."}
                </p>
              </section>
            </div>
            <footer className="page-footer">
              <span>
                ENGINEERING HUB <span className="footer-separator">/</span>{" "}
                PRIVATE CONTROL PLANE
              </span>
              <span>Code, staging & production stay separate.</span>
            </footer>
          </>
        )}
      </main>
      {newTask && (
        <Modal title="Assign a task" onClose={() => setNewTask(false)}>
          <form onSubmit={(event) => void saveTask(event)}>
            <p className="modal-copy">
              Define the goal. An agent can claim it and request independent
              review. This does not activate a deployment.
            </p>
            <label className="field">
              Project
              <select
                value={taskProject}
                onChange={(event) =>
                  setTaskProject(event.target.value as ProjectId)
                }
              >
                {data?.projects.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              What needs to happen?
              <textarea
                autoFocus
                value={taskTitle}
                onChange={(event) => setTaskTitle(event.target.value)}
                maxLength={200}
                required
                rows={3}
                placeholder="A concrete, testable outcome…"
              />
            </label>
            <div className="modal-actions">
              <button
                className="secondary"
                type="button"
                onClick={() => setNewTask(false)}
              >
                Cancel
              </button>
              <button
                className="primary"
                disabled={!writable || !taskTitle.trim()}
              >
                Assign task
              </button>
            </div>
          </form>
        </Modal>
      )}
      {pauseChoice !== null && (
        <Modal
          title={
            pauseChoice ? "Pause new staging work?" : "Resume new staging work?"
          }
          onClose={() => setPauseChoice(null)}
        >
          <p className="modal-copy">
            This changes admission of new staging work. Active actions reach a
            safe boundary; the MCP and production are not stopped. It does not
            enable an unconfigured executor.
          </p>
          <div className="modal-actions">
            <button className="secondary" onClick={() => setPauseChoice(null)}>
              Cancel
            </button>
            <button
              className="primary"
              disabled={!writable}
              onClick={async () => {
                const ok = await mutate(
                  () => client.setPause(pauseChoice, session!.csrf_token!),
                  pauseChoice
                    ? "New staging work paused."
                    : "New staging work resumed.",
                );
                if (ok) setPauseChoice(null);
              }}
            >
              {pauseChoice ? "Pause new work" : "Resume new work"}
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
