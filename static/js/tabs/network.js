import { ChartGroup, LineChart } from "../chart.js";
import { badge, dataTable, el, fmtAgo, fmtBytes, fmtNum, fmtRate, panel } from "../util.js";

const EXPOSURE = { "all interfaces": "warn", public: "warn", private: "", tailnet: "ok", localhost: "ok" };

export default {
  id: "network",
  title: "Network",

  create(ctx) {
    const group = new ChartGroup(ctx);
    let chartBuilt = false;
    const chartHolder = el("div");
    const onlyExposed = el("input", { type: "checkbox", id: "only-exposed" });
    let ports = [];

    const ifaces = dataTable({
      sortKey: "name", sortDir: 1,
      columns: [
        { key: "name", label: "Interface", render: (r) => [el("b", null, r.name), " ", badge(r.up ? "up" : "down", r.up ? "ok" : "crit")] },
        { key: "addresses", label: "Addresses", cls: "mono wrap", sortable: false, render: (r) => (r.addresses || []).join("  ") },
        { key: "rx_Bps", label: "In", num: true, render: (r) => fmtRate(r.rx_Bps) },
        { key: "tx_Bps", label: "Out", num: true, render: (r) => fmtRate(r.tx_Bps) },
        { key: "pps", label: "Packets/s in · out", num: true, sortable: false, render: (r) => `${fmtNum(r.rx_pps, 0)} · ${fmtNum(r.tx_pps, 0)}` },
        { key: "total", label: "Total in · out", num: true, sortable: false, render: (r) => `${fmtBytes(r.rx_bytes)} · ${fmtBytes(r.tx_bytes)}` },
        { key: "errors", label: "Errors · drops", num: true, sortable: false,
          render: (r) => (r.rx_errors + r.tx_errors + r.rx_drops + r.tx_drops ? el("span", { class: "err" }, `${r.rx_errors + r.tx_errors} · ${r.rx_drops + r.tx_drops}`) : "0") },
      ],
    });
    const listening = dataTable({
      sortKey: "port", sortDir: 1, empty: "No listening sockets",
      columns: [
        { key: "port", label: "Port", num: true },
        { key: "proto", label: "Protocol" },
        { key: "address", label: "Bound to", cls: "mono" },
        { key: "exposure", label: "Reachable from", render: (r) => badge(r.exposure, EXPOSURE[r.exposure] ?? "") },
        { key: "process", label: "Process", render: (r) => r.process || el("span", { class: "faint" }, "unknown (needs root)") },
      ],
    });
    const remotes = dataTable({
      sortKey: "connections", sortDir: -1, empty: "No outside connections right now",
      columns: [{ key: "ip", label: "Remote address", cls: "mono" }, { key: "connections", label: "Connections", num: true }],
    });
    const peers = dataTable({
      sortKey: "online", sortDir: -1, empty: "No other devices",
      columns: [
        { key: "name", label: "Device", render: (r) => [el("b", null, r.name), r.exit_node ? [" ", badge("exit node")] : null, el("div", { class: "sub" }, r.dns)] },
        { key: "online", label: "Status", value: (r) => (r.online ? 1 : 0), render: (r) => badge(r.online ? "online" : "offline", r.online ? "ok" : "") },
        { key: "os", label: "System" },
        { key: "ips", label: "Addresses", cls: "mono wrap", sortable: false, render: (r) => r.ips.join("  ") },
        { key: "connection", label: "Path", render: (r) => r.connection || "–" },
        { key: "last_seen", label: "Last seen", num: true, render: (r) => (r.online ? "now" : fmtAgo(r.last_seen)) },
        { key: "traffic", label: "Traffic in · out", num: true, sortable: false, render: (r) => `${fmtBytes(r.rx_bytes)} · ${fmtBytes(r.tx_bytes)}` },
      ],
    });

    const ifPanel = panel("Interfaces"), portPanel = panel("Listening ports", el("label", { class: "check", for: "only-exposed" }, onlyExposed, "only reachable from outside"));
    const connPanel = panel("Connections"), tsPanel = panel("Tailscale");
    ifPanel.set(ifaces.el); portPanel.set(listening.el);
    const chips = el("div", { class: "chips" });
    connPanel.set(chips, el("div", { class: "rows" }, remotes.el));
    const note = (p, s) => { if (!s) p.set(el("p", { class: "empty" }, "Waiting for data…")); else if (s.data.error) p.set(el("p", { class: "err" }, `Unavailable: ${s.data.error}`)); else return true; return false; };

    const paintPorts = () => listening.setRows(onlyExposed.checked ? ports.filter((p) => p.exposure !== "localhost") : ports);
    onlyExposed.addEventListener("change", paintPorts);

    return {
      el: el("div", { class: "rows" },
        el("div", { class: "toolbar" }, el("h2", { class: "brand" }, "Traffic"), el("span", { class: "spacer" }), ctx.rangePicker()),
        chartHolder, ifPanel.el, portPanel.el, connPanel.el, tsPanel.el),
      onRange() { group.refresh(true); },
      update(live) {
        const s = live.sections;
        if (!chartBuilt && s.net_io && !s.net_io.data.error) {
          chartBuilt = true;
          const names = s.net_io.data.interfaces.map((i) => i.name);
          chartHolder.append(group.add(new LineChart({ title: "Throughput", unit: "rate", series: names.flatMap((n, i) => [
            { metric: `net.${n}.rx_Bps`, label: `${n} in`, color: (i * 2) % 6 + 1 }, { metric: `net.${n}.tx_Bps`, label: `${n} out`, color: (i * 2 + 1) % 6 + 1 }]) })).el);
        }
        group.refresh();

        const addrs = new Map(s.sockets && !s.sockets.data.error ? s.sockets.data.addresses.map((a) => [a.name, a.addresses]) : []);
        if (note(ifPanel, s.net_io)) { ifPanel.set(ifaces.el); ifaces.setRows(s.net_io.data.interfaces.map((i) => ({ ...i, addresses: addrs.get(i.name) }))); }

        if (note(portPanel, s.sockets)) { portPanel.set(listening.el); ports = s.sockets.data.listening; paintPorts(); }
        if (note(connPanel, s.sockets)) {
          const d = s.sockets.data;
          chips.replaceChildren(...Object.entries(d.tcp_states).map(([k, n]) => badge(`${n} ${k.toLowerCase().replace("_", " ")}`, k === "ESTABLISHED" ? "ok" : "")));
          connPanel.set(chips, el("div", { class: "sub" }, "Established connections from other machines, by address"), remotes.el);
          remotes.setRows(d.top_remotes);
        }
        if (note(tsPanel, s.tailscale)) {
          const t = s.tailscale.data;
          tsPanel.set(el("div", { class: "chips" },
            badge(t.state, t.state === "Running" ? "ok" : "crit"), badge(`this server: ${t.self.name}`), badge(t.self.ips.join("  ")), badge(`v${t.version}`), badge(t.magic_dns_suffix)),
            el("div", { class: "rows" }, peers.el));
          peers.setRows(t.peers);
        }
      },
    };
  },
};
