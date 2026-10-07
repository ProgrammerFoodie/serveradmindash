// Stacked daily bars: one column per day, one coloured segment per agent type.
// Segment heights are set through the CSSOM (element.style), which the page's CSP allows; style="" attributes it would block.

import { el, fmtDuration, fmtNum, fmtTokens } from "./util.js";

export const colorClass = (index) => `c${(index % 6) + 1}`;

export class AgentChart {
  /** mode: "tokens" or "seconds". */
  constructor({ mode = "tokens" } = {}) {
    this.mode = mode;
    this.bars = el("div", { class: "agent-chart", role: "list" });
    this.first = el("span"); this.last = el("span");
    this.legend = el("div", { class: "legend" });
    this.detail = el("div", { class: "agent-detail sub" }, "Hover or focus a day to see its numbers.");
    this.el = el("div", null, this.bars, el("div", { class: "agent-axis" }, this.first, this.last), this.legend, this.detail);
    this.data = null;
  }

  value(a, i) { return this.mode === "tokens" ? a.tokens[i] : a.seconds[i]; }
  format(v) { return this.mode === "tokens" ? `${fmtTokens(v)} tokens` : fmtDuration(v); }

  setMode(mode) { this.mode = mode; if (this.data) this.setData(this.data); }

  setData(data) {
    this.data = data;
    const names = Object.keys(data.agents).sort((x, y) => data.agents[y].tokens.reduce((a, b) => a + b, 0) - data.agents[x].tokens.reduce((a, b) => a + b, 0));
    const total = (i) => names.reduce((s, n) => s + this.value(data.agents[n], i), 0);
    const max = Math.max(1, ...data.days.map((_, i) => total(i)));
    this.bars.replaceChildren(...data.days.map((day, i) => {
      const col = el("div", { class: "agent-day", role: "listitem", tabindex: "0", "aria-label": `${day}: ${this.format(total(i))}` },
        names.filter((n) => this.value(data.agents[n], i) > 0).map((n) => {
          const seg = el("i", { class: `agent-seg ${colorClass(names.indexOf(n))}` });
          seg.style.height = `${(this.value(data.agents[n], i) / max) * 100}%`;
          return seg;
        }));
      const show = () => this.detail.replaceChildren(day + ": ", ...(names.some((n) => this.value(data.agents[n], i) > 0)
        ? names.filter((n) => this.value(data.agents[n], i) > 0).map((n) => el("span", { class: "mono" }, `${n} ${this.format(this.value(data.agents[n], i))} (${fmtNum(data.agents[n].runs_per_day[i])} runs)   `))
        : ["no runs"]));
      col.addEventListener("mouseenter", show); col.addEventListener("focus", show);
      return col;
    }));
    this.first.textContent = data.days[0] ?? ""; this.last.textContent = data.days[data.days.length - 1] ?? "";
    this.legend.replaceChildren(...names.map((n) => el("span", { class: colorClass(names.indexOf(n)) }, n)));
    return names;
  }
}
