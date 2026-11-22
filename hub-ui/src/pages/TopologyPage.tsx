import { useEffect, useMemo, useRef, useState } from "react";
import { HubApiError } from "../api";
import type { Hub } from "../hub";
import { httpInfrastructure, type InfrastructureClient } from "../infrastructureApi";
import { demoInfrastructure } from "../infrastructureDemo";
import { observationView, type Device, type DeviceList } from "../infrastructureModel";
import { filterTopologyGroups, groupDevicesBySubnet } from "../topologyModel";
import { href } from "../router";
import { Badge, PageHeader, PanelTitle } from "../ui";
import "./TopologyPage.css";

type Load<T> = { state: "loading" } | { state: "loaded"; value: T } | { state: "failed"; message: string } | { state: "unavailable" };
const UNAVAILABLE = "Infrastructure isn't available to you on this Hub.";

function useClient(hub: Hub): InfrastructureClient {
  return useMemo(() => hub.demo ? demoInfrastructure() : httpInfrastructure(), [hub.demo]);
}

function useDeviceList(hub: Hub, client: InfrastructureClient): [Load<DeviceList>, () => void] {
  const [load, setLoad] = useState<Load<DeviceList>>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);
  const read = useRef(client.list);
  read.current = client.list;
  useEffect(() => {
    const controller = new AbortController();
    setLoad(current => current.state === "loaded" ? current : { state: "loading" });
    // Include retired entries: this view is a complete picture of registered Inventory records.
    read.current(controller.signal, true).then(value => {
      if (!controller.signal.aborted) setLoad({ state: "loaded", value });
    }).catch(error => {
      if (controller.signal.aborted) return;
      if (error instanceof HubApiError && error.status === 401) { hub.clearPrivateWorkspace(); return; }
      if (error instanceof HubApiError && [403, 404, 503].includes(error.status) && error.code !== "device_not_found") {
        setLoad({ state: "unavailable" }); return;
      }
      setLoad({ state: "failed", message: "The Hub's registered devices couldn't be read. Use Refresh to try again." });
    });
    return () => controller.abort();
  }, [client, hub.clearPrivateWorkspace, hub.data, attempt]);
  return [load, () => setAttempt(value => value + 1)];
}

function useNow(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 15_000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

export function TopologyPage({ hub, actions }: { hub: Hub; actions: React.ReactNode }) {
  const client = useClient(hub);
  const now = useNow();
  const [load, refresh] = useDeviceList(hub, client);
  const [query, setQuery] = useState("");
  const list = load.state === "loaded" ? load.value : null;
  const groups = useMemo(() => groupDevicesBySubnet(list?.items ?? []), [list]);
  const shown = useMemo(() => filterTopologyGroups(groups, query), [groups, query]);
  const deviceCount = list?.items.length;
  const subnetCount = groups.filter(group => group.network !== null).length;

  return <>
    <PageHeader eyebrow="INFRASTRUCTURE" title="Topology"
      subtitle="Declared subnets and the devices recorded on each one. This view is read-only; nothing here scans or changes a network.">
      {actions}
    </PageHeader>
    <p className="infra-back"><a href={href({ page: "infrastructure" })}>Back to Infrastructure</a></p>
    {load.state === "unavailable" ? <section className="panel"><p>{UNAVAILABLE}</p></section> :
      <section className="panel topology" aria-label="Topology">
        <PanelTitle title="Registered device topology" count={deviceCount}>
          <button type="button" className="secondary" disabled={load.state === "loading"} onClick={refresh}>
            {load.state === "loading" ? "Reading…" : "Refresh"}
          </button>
        </PanelTitle>
        <p className="topology-source"><strong>Source and coverage:</strong> Engineering Hub Inventory records only. This page does not scan the network; unregistered devices won't appear. A device remains listed even when it has no Prometheus mapping or Prometheus has no data for it. Groups use only the device's declared CIDR; addresses are never used to guess a subnet.</p>
        {hub.demo && <p role="status">Demo: illustrative devices and documentation-only addresses. Nothing is read from a real network.</p>}
        {list && <p className="topology-count" role="status">{deviceCount} registered {deviceCount === 1 ? "device" : "devices"} across {subnetCount} declared {subnetCount === 1 ? "subnet" : "subnets"}, plus Unassigned where applicable.</p>}
        <label className="topology-filter">
          <span>Filter subnets and devices</span>
          <input type="search" aria-label="Filter topology" value={query} onChange={event => setQuery(event.target.value)} placeholder="Name, address, role, VLAN or CIDR" />
        </label>
        {load.state === "loading" && !list && <p role="status">Reading registered devices…</p>}
        {load.state === "failed" && <p className="notice error" role="alert">{load.message}</p>}
        {list && list.items.length === 0 && <p className="infra-empty">No devices are recorded in Inventory yet.</p>}
        {list && list.items.length > 0 && shown.length === 0 && <p className="infra-empty">No registered devices match this filter.</p>}
        {shown.length > 0 && <div className="topology-groups">
          {shown.map(group => {
            const unassigned = group.network === null;
            return <section key={group.network ?? "unassigned"} className="topology-group"
              aria-label={unassigned ? "Unassigned devices" : `${group.network} subnet`}>
              <div className="topology-group-head">
                <h3>{unassigned ? "Unassigned" : group.network}</h3>
                <span>{group.devices.length} {group.devices.length === 1 ? "device" : "devices"}</span>
              </div>
              <ul className="topology-devices">
                {group.devices.map(device => <TopologyDevice key={device.id} device={device} now={now} />)}
              </ul>
            </section>;
          })}
        </div>}
        {list && <p className="topology-coverage">Showing {deviceCount} registered Inventory devices{list.items.some(device => device.retired) ? ", including retired records" : ""}. Missing Prometheus data does not remove a device from this view.</p>}
      </section>}
  </>;
}

function TopologyDevice({ device, now }: { device: Device; now: Date }) {
  return <li className={device.retired ? "topology-device retired" : "topology-device"}>
    <div className="topology-device-main">
      <a href={href({ page: "device", id: device.id })}>{device.name}</a>
      <span>{[device.hostname, ...device.addresses].filter(Boolean).join(" · ") || "No address recorded"}</span>
    </div>
    <div className="topology-device-facts">
      <span><strong>Role:</strong> {device.role}</span>
      {device.vlan !== null && <span className="infra-chip">VLAN {device.vlan}</span>}
      {device.retired && <span className="infra-chip">Retired</span>}
      {device.open_maintenance && <span className="infra-chip">Maintenance open</span>}
    </div>
    <div className="topology-device-checks" aria-label={`${device.name} checks`}>
      {device.observations.length === 0
        ? <Badge tone="neutral">Unknown: no checks set up</Badge>
        : device.observations.map(item => {
          const view = observationView(item, now);
          return <Badge key={item.probe_id} tone={view.tone}>{view.label}: {view.line}</Badge>;
        })}
    </div>
  </li>;
}
