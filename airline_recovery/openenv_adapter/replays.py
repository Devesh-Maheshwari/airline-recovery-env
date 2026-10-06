"""Read-only replay viewer for the recorded hard-tier episodes, served at /replays."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

DATA = Path(__file__).with_name("replays.json")

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent replays</title>
<style>
:root{--bg:#fbfbfa;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e3de;--pass:#1f7a4d;--passbg:#e6f4ec;--fail:#b3261e;--failbg:#fbe9e7;--warn:#8a5a00;--warnbg:#fff4dc;--accent:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1d1d1b;--ink:#ecebe6;--muted:#a3a29b;--line:#34332f;--pass:#6fd39b;--passbg:#173326;--fail:#ff8a80;--failbg:#3a1d1a;--warn:#f2c46b;--warnbg:#3a2e14;--accent:#8fb0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}
p.lead{color:var(--muted);margin:0 0 18px;max-width:760px}a{color:var(--accent)}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600}
td.n{text-align:right;font-variant-numeric:tabular-nums}tr.pick{cursor:pointer}tr.pick:hover td{background:color-mix(in srgb,var(--accent) 7%,transparent)}tr.sel td{background:color-mix(in srgb,var(--accent) 12%,transparent)}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600}.pass{color:var(--pass);background:var(--passbg)}.fail{color:var(--fail);background:var(--failbg)}.warn{color:var(--warn);background:var(--warnbg)}
.controls{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 12px}select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)}
.layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);gap:16px;align-items:start}@media (max-width:900px){.layout{grid-template-columns:1fr}}
.list{max-height:70vh;overflow:auto;border-radius:8px}.panel{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;position:sticky;top:12px}
.harm{margin:8px 0 12px;padding:10px 12px;border-radius:8px;background:var(--failbg);color:var(--fail)}.ok{margin:8px 0 12px;padding:10px 12px;border-radius:8px;background:var(--passbg);color:var(--pass)}
ol.steps{margin:0;padding:0;list-style:none;max-height:56vh;overflow:auto}ol.steps li{display:grid;grid-template-columns:34px 1fr;gap:8px;padding:7px 0;border-bottom:1px solid var(--line)}
.num{color:var(--muted);font-variant-numeric:tabular-nums;text-align:right}.tool{font-weight:600}.args{font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);word-break:break-all;margin-top:2px}
.state{font-size:12px;color:var(--muted);margin-top:2px}.err{font-size:12px;color:var(--fail);margin-top:2px}.empty{color:var(--muted);padding:24px 0}
.cta{display:inline-block;padding:8px 14px;border-radius:8px;background:var(--accent);color:var(--bg);text-decoration:none;font-weight:600}td{white-space:nowrap}td:first-child{white-space:normal}
.meta{display:flex;flex-wrap:wrap;gap:6px 16px;color:var(--muted);font-size:13px;margin:4px 0 0}
</style></head><body><main>
<h1>Airline Recovery Env: agent replays</h1>
<p class="lead">A pretend airline runs as five small services: pricing, seats, payments, bookings and check-in. Something breaks, for example a payment goes through but the booking is never told. An AI agent gets tools to look around and repair it, and is graded only on what happened to the customers: nobody charged twice, nobody who cancelled given a ticket, every paying customer checked in.</p>
<p class="lead">Below is every recorded episode on the hard tier: a reference procedure that always solves it, and three AI coding agents on the same incidents. Pick an episode to see each move and what the grader found.</p>
<p><a class="cta" href="/web/">Play it yourself</a> <a href="https://github.com/Devesh-Maheshwari/airline-recovery-env" style="margin-left:12px">Source and docs on GitHub</a></p>
<h2>Results</h2><table id="summary"></table>
<h2>Episodes</h2>
<div class="controls"><select id="agent"></select><select id="outcome"><option value="">All outcomes</option><option value="pass">Solved</option><option value="harm">Harmed a customer</option><option value="other">Other failure</option></select><select id="level"><option value="">All levels</option><option>1</option><option>2</option><option>3</option></select></div>
<div class="layout"><div class="list"><table id="episodes"></table></div><div class="panel" id="detail"><div class="empty">Select an episode on the left.</div></div></div>
</main><script>
const HARM={duplicate_charge:"Charged a customer twice",cancelled_request_fulfilled:"Gave a ticket to a customer who had cancelled",duplicate_sale:"Sold one customer two seats",
accepted_request_changed:"Booked a customer at a different price than promised",unfunded_confirmation:"Confirmed a booking whose payment was missing or doubled",oversold:"Sold more seats than the plane has",
rejected_request_fulfilled:"Sold a seat on a sold-out flight",refund_missing:"Cancelled a paid booking without refunding it",refund_unwarranted:"Refunded a booking that was not cancelled",
cancelled_booking_backed:"Left a seat or check-in on a cancelled booking",incorrect_charge:"Charged the wrong amount",orphan_charge:"Charge with no booking",unbacked_confirmation:"Confirmed a booking with no seat or payment",
invalid_hold:"Seat held for the wrong passenger",ineligible_checkin:"Checked in a booking that was not confirmed",preincident_booking_modified:"Changed a booking made before the incident",valid_event_discarded:"Threw away a valid check-in event"};
const TOOL={get_metrics:"Check service health",get_logs:"Read request logs",get_config:"Read settings",patch_config:"Change settings",restart_service:"Restart a service",query_sql:"Look at the database",
replay_events:"Deliver queued check-in events",quarantine_event:"Set aside a broken event",invalidate_cache:"Clear cached prices",reconcile_booking:"Complete a stuck booking",provider_lookup:"Ask the payment provider",
void_booking:"Cancel a booking (and refund)",probe:"Test with fresh customer traffic",finish:"Declare it fixed"};
let D,cur=null;const $=id=>document.getElementById(id),esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const kind=e=>e.success?"pass":(e.violations.length?"harm":"other");
function badge(e){const k=kind(e);return k==="pass"?'<span class="badge pass">Solved</span>':k==="harm"?'<span class="badge fail">Harm</span>':'<span class="badge warn">Not finished</span>'}
fetch("/replays/data.json").then(r=>r.json()).then(d=>{D=d;init()});
function init(){const a=$("agent");a.innerHTML='<option value="">All agents</option>'+(D.agent_order||Object.keys(D.agents)).map(k=>`<option value="${k}">${esc(D.agents[k])}</option>`).join("");
let rows="<tr><th>Agent</th><th class=n>Solved</th><th class=n>Harmed a customer</th><th class=n>Other failure</th><th>Level 1 / 2 / 3 solved</th></tr>";
for(const k of (D.agent_order||Object.keys(D.agents))){const name=D.agents[k];const es=D.episodes.filter(e=>e.agent===k);const lv=[1,2,3].map(l=>{const x=es.filter(e=>e.level===l);return `${x.filter(e=>e.success).length}/${x.length}`}).join(" · ");
rows+=`<tr><td>${esc(name)}</td><td class=n>${es.filter(e=>e.success).length} / ${es.length}</td><td class=n>${es.filter(e=>kind(e)==="harm").length}</td><td class=n>${es.filter(e=>kind(e)==="other").length}</td><td>${lv}</td></tr>`}
$("summary").innerHTML=rows;["agent","outcome","level"].forEach(id=>$(id).onchange=list);list();
const m=location.hash.slice(1).split("/");if(m.length===3){const i=D.episodes.findIndex(e=>e.agent===m[0]&&e.task===m[1]&&String(e.seed)===m[2]);if(i>=0)show(i)}}
function list(){const a=$("agent").value,o=$("outcome").value,l=$("level").value;const es=D.episodes.filter(e=>(!a||e.agent===a)&&(!o||kind(e)===o)&&(!l||String(e.level)===l));
let rows="<tr><th>Agent</th><th>Task</th><th class=n>Seed</th><th class=n>Level</th><th>Outcome</th><th class=n>Moves</th></tr>";
es.forEach(e=>{const i=D.episodes.indexOf(e);rows+=`<tr class="pick${i===cur?" sel":""}" data-i="${i}"><td>${esc(D.agents[e.agent])}</td><td>${e.task}</td><td class=n>${e.seed}</td><td class=n>${e.level}</td><td>${badge(e)}</td><td class=n>${e.steps_taken??"—"}</td></tr>`});
$("episodes").innerHTML=es.length?rows:'<tr><td class="empty">No episodes match.</td></tr>';document.querySelectorAll("tr.pick").forEach(r=>r.onclick=()=>show(+r.dataset.i))}
function show(i){cur=i;const e=D.episodes[i];history.replaceState(null,"",`#${e.agent}/${e.task}/${e.seed}`);list();let h=`<h2 style="margin-top:0">${esc(D.agents[e.agent])} · hard ${e.task} · seed ${e.seed}</h2>`;
h+=`<div class="meta"><span>Level ${e.level}</span><span>${e.steps_taken??"?"} of ${e.budget??"?"} moves</span>${e.truncated?"<span>Ran out of moves</span>":""}${e.finished===false?"<span>Never declared it fixed</span>":""}</div>`;
if(e.success)h+='<div class="ok">Solved: every customer ended up with the right outcome, and nobody was harmed.</div>';
else if(e.violations.length){h+='<div class="harm"><b>Harmed customers:</b><ul style="margin:4px 0 0 18px;padding:0">'+e.violations.map(v=>`<li>${esc(HARM[v]||v)}</li>`).join("")+"</ul>"+(e.excess_capture_cents?`<div style="margin-top:4px">Extra money taken: $${(e.excess_capture_cents/100).toFixed(2)} (synthetic)</div>`:"")+"</div>"}
else h+=`<div class="harm" style="background:var(--warnbg);color:var(--warn)">Not solved, but no customer was harmed: ${e.verified===false?"it never confirmed the fix with test traffic":"the recovery was incomplete"}.</div>`;
h+='<ol class="steps">'+e.steps.map((s,n)=>{const args=Object.keys(s.arguments||{}).length?esc(JSON.stringify(s.arguments)):"";
const st=s.pending!==undefined?`<div class="state">after: ${s.pending} stuck bookings · ${s.outbox} queued events · ${s.verified}/2 checks passed</div>`:"";
const er=s.ok===false&&s.error?`<div class="err">${esc(s.error)}</div>`:"";
return `<li><span class="num">${n+1}</span><div><span class="tool">${esc(TOOL[s.tool]||s.tool)}</span> <span style="color:var(--muted);font-size:12px">${esc(s.tool)}</span>${args?`<div class="args">${args}</div>`:""}${st}${er}</div></li>`}).join("")+"</ol>";
if(e.agent!=="oracle")h+='<p style="color:var(--muted);font-size:13px;margin-bottom:0">Coding-agent episodes record the moves the agent sent; the system\'s replies are not kept.</p>';
$("detail").innerHTML=h}
</script></body></html>"""


def register(app: FastAPI) -> None:
    """Add the replay page and its data to an existing app."""
    @app.get("/replays", include_in_schema=False)
    @app.get("/replays/", include_in_schema=False)
    def replay_page() -> HTMLResponse:
        return HTMLResponse(PAGE)

    @app.get("/replays/data.json", include_in_schema=False)
    def replay_data():
        if not DATA.exists():
            return JSONResponse({"version": 1, "agents": {}, "episodes": []})
        return FileResponse(DATA, media_type="application/json")
