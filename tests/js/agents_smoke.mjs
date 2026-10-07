// Renders the Agents tab against the fake DOM with empty and populated data.
let failed = 0;
const check = (ok, label) => { if (!ok) { console.log(`FAIL ${label}`); failed++; } };

import { installFakeDom } from "./fakedom.mjs";
installFakeDom();
const { default: agents } = await import("../../static/js/tabs/agents.js");

const days = Array.from({ length: 30 }, (_, i) => `2026-09-${String(i + 1).padStart(2, "0")}`);
const zeros = () => new Array(30).fill(0);
const a = { runs: 3, tokens: zeros(), output: zeros(), seconds: zeros(), runs_per_day: zeros() };
a.tokens[29] = 1500000; a.output[29] = 4000; a.seconds[29] = 5400; a.runs_per_day[29] = 3;
const b = { runs: 1, tokens: zeros(), output: zeros(), seconds: zeros(), runs_per_day: zeros() };
b.tokens[28] = 800; b.output[28] = 800; b.seconds[28] = 20; b.runs_per_day[28] = 1;
const data = { days, timezone: "UTC", scanned: 4, agents: { Explore: b, "general-purpose": a },
  recent: [{ agent: "Explore", descr: "Survey", model: "m", start: 1788703764, seconds: 20, tokens: 800 }],
  totals: { runs: 4, tokens: 1500800, seconds: 5420 } };
const empty = { days, timezone: "UTC", scanned: 0, agents: {}, recent: [], totals: { runs: 0, tokens: 0, seconds: 0 } };

const tab = agents.create({});
const text = () => tab.el.textContent;

tab.update({ sections: { agents: null } });
check(text().includes("Waiting for data"), "waiting state");
tab.update({ sections: { agents: { data: { error: "boom" } } } });
check(text().includes("boom"), "error state");
tab.update({ sections: { agents: { data: empty } } });
check(text().includes("No sub-agent runs in the last 30 days"), "empty state");

tab.update({ sections: { agents: { data } } });
check(text().includes("general-purpose") && text().includes("Explore"), "agent names");
check(text().includes("1.50M"), "token formatting");
check(text().includes("2026-09-01") && text().includes("2026-09-30"), "axis dates");
const segs = tab.el.find((n) => n.hasClass("agent-seg"));
check(segs.length === 2, `two segments, got ${segs.length}`);
check(segs.some((s) => s.style.height === "100%"), "tallest segment fills the bar");
const hours = tab.el.find((n) => n.tag === "button" && n.textContent === "Hours")[0];
hours.click();
check(tab.el.find((n) => n.hasClass("agent-seg")).length === 2, "hours mode keeps both segments");
check(hours.getAttribute("aria-pressed") === "true", "hours button pressed");
check(!/\b(null|undefined|NaN)\b/.test(text()), "no null/undefined/NaN text");

// same collection time: nothing is rebuilt (a rebuild would drop keyboard focus); a new time rebuilds
const t1 = { sections: { agents: { t: 100, data } } };
tab.update(t1);
const col = () => tab.el.find((n) => n.hasClass("agent-day"))[0];
const before = col();
tab.update(t1);
check(col() === before, "identical data does not rebuild the chart");
tab.update({ sections: { agents: { t: 160, data } } });
check(col() !== before, "new data rebuilds the chart");

// an error blanks all three panels, and recovery brings the tables back
tab.update({ sections: { agents: { data: { error: "boom" } } } });
check(!text().includes("general-purpose"), "no stale agent rows while unavailable");
tab.update({ sections: { agents: { t: 220, data } } });
check(text().includes("general-purpose") && text().includes("Survey"), "tables return after recovery");

if (failed) process.exit(1);
console.log("ok");
