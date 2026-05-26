"""Generate competitor analysis dashboard as a self-contained HTML file.

Reads:
  data/ads/google_ads.json
  data/sheets_ready/promotions_merged_for_sheets.json

Writes:
  dashboard.html  (open in any browser, no server needed)

Usage:
    python run_dashboard.py
    python run_dashboard.py --open   # open in browser after generating
"""
import argparse
import json
import sys
import webbrowser
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ADS_FILE   = ROOT / "data" / "ads" / "google_ads.json"
PROMOS_FILE = ROOT / "data" / "sheets_ready" / "promotions_merged_for_sheets.json"
OUT_FILE   = ROOT / "dashboard.html"

PALETTE = [
    "#6366f1","#d97706","#059669","#dc2626",
    "#2563eb","#7c3aed","#db2777","#0d9488",
    "#ea580c","#65a30d","#0891b2",
]


def load_data():
    ads, promos = [], []
    if ADS_FILE.exists():
        ads = json.loads(ADS_FILE.read_text(encoding="utf-8")).get("ads", [])
    if PROMOS_FILE.exists():
        promos = json.loads(PROMOS_FILE.read_text(encoding="utf-8")).get("rows", [])
    return ads, promos


def build_html(ads, promos):
    ads_json    = json.dumps(ads,    ensure_ascii=False)
    promos_json = json.dumps(promos, ensure_ascii=False)
    palette_json = json.dumps(PALETTE)
    today = date.today().strftime("%B %d, %Y")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Competitor Intelligence</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet"/>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/motion@10.18.0/dist/motion.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#f8fafc;--surface:#fff;--border:#e2e8f0;
  --text:#0f172a;--muted:#64748b;--subtle:#f1f5f9;
  --accent:#6366f1;--accent-bg:#eef2ff;
  --shadow:0 1px 3px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04);
  --shadow-md:0 4px 12px rgba(0,0,0,.08),0 2px 4px rgba(0,0,0,.04);
}}
body{{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}}

/* ── Header ── */
header{{
  position:sticky;top:0;z-index:100;
  background:rgba(255,255,255,.85);backdrop-filter:blur(12px);
  border-bottom:1px solid var(--border);
  padding:12px 28px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;
}}
.header-left h1{{font-size:16px;font-weight:700;letter-spacing:-.3px;color:var(--text)}}
.header-left span{{font-size:11px;color:var(--muted);display:block;margin-top:1px}}
.header-right{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.filter-group{{display:flex;align-items:center;gap:6px}}
.filter-group label{{font-size:11px;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}}
select{{
  background:var(--surface);color:var(--text);
  border:1px solid var(--border);border-radius:8px;
  padding:6px 10px;font-size:12px;font-family:inherit;
  cursor:pointer;outline:none;transition:border-color .15s,box-shadow .15s;
  appearance:none;-webkit-appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2394a3b8' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 8px center;padding-right:26px;
}}
select:hover{{border-color:#94a3b8}}
select:focus{{border-color:var(--accent);box-shadow:0 0 0 3px #eef2ff}}
.chip{{
  display:inline-flex;align-items:center;gap:5px;
  background:var(--accent-bg);color:var(--accent);
  border:1px solid #c7d2fe;border-radius:20px;
  padding:4px 10px;font-size:11px;font-weight:600;cursor:pointer;
  transition:background .15s;
}}
.chip:hover{{background:#e0e7ff}}
.chip .x{{font-size:13px;line-height:1;opacity:.7}}
#activeChip{{display:none}}

/* ── Layout ── */
main{{padding:24px 28px;max-width:1440px;margin:0 auto}}
.grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}}
.grid-3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:16px}}
.grid-5{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:20px}}
@media(max-width:1024px){{.grid-3{{grid-template-columns:1fr 1fr}}}}
@media(max-width:720px){{.grid-2,.grid-3,.grid-5{{grid-template-columns:1fr}}}}

/* ── Cards ── */
.card{{
  background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:20px;
  box-shadow:var(--shadow);
  transition:box-shadow .2s,transform .2s;
  opacity:0;transform:translateY(16px);  /* pre-animation state */
}}
.card:hover{{box-shadow:var(--shadow-md);transform:translateY(-1px)}}
.card h2{{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:16px}}

/* ── KPI ── */
.kpi{{
  background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:18px 20px;
  box-shadow:var(--shadow);position:relative;overflow:hidden;
  transition:box-shadow .2s,transform .2s;
  opacity:0;transform:translateY(16px);
}}
.kpi:hover{{box-shadow:var(--shadow-md);transform:translateY(-1px)}}
.kpi::before{{
  content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:var(--kpi-color,var(--accent));border-radius:12px 12px 0 0;
}}
.kpi .val{{font-size:30px;font-weight:700;line-height:1;margin-bottom:4px;color:var(--text)}}
.kpi .lbl{{font-size:12px;color:var(--muted);font-weight:500}}
.kpi .sub{{font-size:11px;color:#94a3b8;margin-top:6px}}

/* ── Spotlight ── */
.spotlight{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px}}
@media(max-width:720px){{.spotlight{{grid-template-columns:1fr}}}}
.deal-card{{
  background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:16px 18px;
  box-shadow:var(--shadow);position:relative;
  transition:box-shadow .2s,transform .2s;
  opacity:0;transform:translateY(16px);
}}
.deal-card:hover{{box-shadow:var(--shadow-md);transform:translateY(-2px)}}
.deal-card .disc-badge{{
  display:inline-block;font-size:22px;font-weight:700;
  color:var(--accent);margin-bottom:4px;
}}
.deal-card .comp{{font-size:13px;font-weight:600;color:var(--text)}}
.deal-card .svc{{font-size:12px;color:var(--muted);margin-top:2px}}
.deal-card .desc{{font-size:11px;color:#94a3b8;margin-top:8px;line-height:1.5}}
.deal-rank{{
  position:absolute;top:14px;right:14px;
  width:22px;height:22px;border-radius:50%;
  background:var(--subtle);color:var(--muted);
  font-size:10px;font-weight:700;
  display:flex;align-items:center;justify-content:center;
}}

/* ── Section headers ── */
.section-label{{
  font-size:13px;font-weight:600;color:var(--text);
  margin:24px 0 12px;display:flex;align-items:center;gap:8px;
}}
.section-label .count{{
  font-size:11px;font-weight:500;color:var(--muted);
  background:var(--subtle);border-radius:20px;padding:2px 8px;
}}

/* ── Tables ── */
.table-wrap{{max-height:380px;overflow-y:auto;border-radius:10px;border:1px solid var(--border)}}
.table-wrap::-webkit-scrollbar{{width:4px}}
.table-wrap::-webkit-scrollbar-track{{background:transparent}}
.table-wrap::-webkit-scrollbar-thumb{{background:#cbd5e1;border-radius:4px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
thead tr{{background:var(--subtle)}}
thead th{{
  padding:10px 12px;text-align:left;font-size:10.5px;font-weight:600;
  color:var(--muted);text-transform:uppercase;letter-spacing:.5px;
  position:sticky;top:0;background:var(--subtle);white-space:nowrap;
  cursor:pointer;user-select:none;
}}
thead th:hover{{color:var(--text)}}
thead th.sorted-asc::after{{content:' ↑';color:var(--accent)}}
thead th.sorted-desc::after{{content:' ↓';color:var(--accent)}}
tbody tr{{border-bottom:1px solid #f1f5f9;transition:background .12s}}
tbody tr:last-child{{border-bottom:none}}
tbody tr:hover{{background:#fafbff}}
tbody td{{padding:9px 12px;vertical-align:middle}}
.badge{{
  display:inline-flex;align-items:center;
  padding:2px 8px;border-radius:20px;font-size:10.5px;font-weight:600;
  white-space:nowrap;
}}
.b-green{{background:#dcfce7;color:#16a34a}}
.b-amber{{background:#fef3c7;color:#d97706}}
.b-blue{{background:#dbeafe;color:#2563eb}}
.b-purple{{background:#f3e8ff;color:#7c3aed}}
.b-red{{background:#fee2e2;color:#dc2626}}
.b-gray{{background:var(--subtle);color:var(--muted)}}
.comp-dot{{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0}}
.comp-cell{{display:flex;align-items:center;gap:7px;font-weight:500}}
.search-input{{
  background:var(--surface);color:var(--text);
  border:1px solid var(--border);border-radius:8px;
  padding:6px 10px;font-size:12px;font-family:inherit;
  outline:none;width:200px;transition:border-color .15s,box-shadow .15s;
}}
.search-input:focus{{border-color:var(--accent);box-shadow:0 0 0 3px #eef2ff}}
.table-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}}
.empty{{color:var(--muted);text-align:center;padding:28px;font-size:13px}}

/* ── Chart tooltip override ── */
#chartTooltip{{
  position:fixed;background:var(--surface);color:var(--text);
  border:1px solid var(--border);border-radius:10px;
  padding:10px 14px;box-shadow:var(--shadow-md);
  pointer-events:none;font-size:12px;z-index:200;
  opacity:0;transition:opacity .12s;min-width:160px;
}}
#chartTooltip .tt-title{{font-weight:600;margin-bottom:6px;font-size:12.5px;color:var(--text)}}
#chartTooltip .tt-row{{display:flex;align-items:center;gap:6px;padding:2px 0;color:var(--muted)}}
#chartTooltip .tt-val{{font-weight:600;color:var(--text);margin-left:auto}}
</style>
</head>
<body>

<header>
  <div class="header-left">
    <h1>Competitor Intelligence</h1>
    <span>Last updated {today}</span>
  </div>
  <div class="header-right">
    <div class="filter-group">
      <label>City</label>
      <select id="cityFilter" onchange="applyFilters()">
        <option value="">All Cities</option>
        <option>Edmonton</option>
        <option>Calgary</option>
        <option>Grande Prairie</option>
      </select>
    </div>
    <div class="filter-group">
      <label>Competitor</label>
      <select id="compFilter" onchange="applyFilters()">
        <option value="">All Competitors</option>
      </select>
    </div>
    <div id="activeChip" class="chip" onclick="clearChartSel()">
      <span id="chipLabel"></span>
      <span class="x">✕</span>
    </div>
  </div>
</header>

<div id="chartTooltip"></div>

<main>
  <!-- KPIs -->
  <div class="grid-5" id="kpiRow"></div>

  <!-- Top deals spotlight -->
  <div class="section-label">Top Discounts <span class="count" id="spotlightCount"></span></div>
  <div class="spotlight" id="spotlightRow"></div>

  <!-- Charts row 1 -->
  <div class="grid-2">
    <div class="card">
      <h2>Promotions per Competitor</h2>
      <canvas id="promosBar" height="210"></canvas>
    </div>
    <div class="card">
      <h2>Google Ads per Competitor</h2>
      <canvas id="adsBar" height="210"></canvas>
    </div>
  </div>

  <!-- Charts row 2 -->
  <div class="grid-3">
    <div class="card">
      <h2>Promo Categories</h2>
      <canvas id="catDonut" height="210"></canvas>
    </div>
    <div class="card">
      <h2>Promos by City</h2>
      <canvas id="cityDonut" height="210"></canvas>
    </div>
    <div class="card">
      <h2>Discount Buckets</h2>
      <canvas id="discBar" height="210"></canvas>
    </div>
  </div>

  <!-- Charts row 3 -->
  <div class="grid-2">
    <div class="card">
      <h2>Ads — Discount vs No Discount</h2>
      <canvas id="stackedBar" height="200"></canvas>
    </div>
    <div class="card">
      <h2>Competitors with Discount Ads</h2>
      <canvas id="discAdsBar" height="200"></canvas>
    </div>
  </div>

  <!-- Deals table -->
  <div class="section-label">
    Active Promotions <span class="count" id="promoCount"></span>
  </div>
  <div class="card" style="padding:16px;margin-bottom:16px">
    <div class="table-header">
      <div style="font-size:11px;color:var(--muted)">Click a chart bar to filter by competitor</div>
      <input class="search-input" id="promoSearch" placeholder="Search promotions…" oninput="renderTable()"/>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th onclick="sortTable('promos','business_name',this)">Competitor</th>
            <th onclick="sortTable('promos','city',this)">City</th>
            <th onclick="sortTable('promos','service_name',this)">Service</th>
            <th onclick="sortTable('promos','discount_value',this)">Discount</th>
            <th>Offer</th>
            <th onclick="sortTable('promos','expiry_date',this)">Expiry</th>
            <th onclick="sortTable('promos','category',this)">Category</th>
          </tr>
        </thead>
        <tbody id="promoTbody"></tbody>
      </table>
    </div>
  </div>

  <!-- Ads table -->
  <div class="section-label">
    Google Ads Copy <span class="count" id="adsCount"></span>
  </div>
  <div class="card" style="padding:16px;margin-bottom:40px">
    <div class="table-header">
      <div style="font-size:11px;color:var(--muted)">All text creatives from Google Ads Transparency Center</div>
      <input class="search-input" id="adsSearch" placeholder="Search ads…" oninput="renderAdsTable()"/>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th onclick="sortTable('ads','business_name',this)">Competitor</th>
            <th onclick="sortTable('ads','ad_title',this)">Headline</th>
            <th>Description</th>
            <th onclick="sortTable('ads','discount_value',this)">Discount</th>
            <th>Domain</th>
          </tr>
        </thead>
        <tbody id="adsTbody"></tbody>
      </table>
    </div>
  </div>
</main>

<script>
// ─── Data ───────────────────────────────────────────────────────────────────
const ADS    = {ads_json};
const PROMOS = {promos_json};
const PALETTE = {palette_json};

// ─── State ──────────────────────────────────────────────────────────────────
let _chartSel = null;   // competitor name clicked on a chart bar
let _sortState = {{ promos: {{ key: null, dir: 1 }}, ads: {{ key: null, dir: 1 }} }};

// ─── Color map ──────────────────────────────────────────────────────────────
const _cm = {{}};
let _ci = 0;
function colorFor(n) {{ if (!_cm[n]) _cm[n] = PALETTE[_ci++ % PALETTE.length]; return _cm[n]; }}
[...new Set([...PROMOS.map(p=>p.business_name),...ADS.map(a=>a.business_name)])].forEach(colorFor);

// ─── Filters ────────────────────────────────────────────────────────────────
function getCity() {{ return document.getElementById('cityFilter').value; }}
function getComp() {{ return document.getElementById('compFilter').value; }}

function filteredPromos() {{
  const city = getCity(), comp = getComp() || _chartSel;
  return PROMOS.filter(p =>
    (!city || (p.city||'').trim() === city) &&
    (!comp || p.business_name === comp)
  );
}}
function filteredAds() {{
  const comp = getComp() || _chartSel;
  return ADS.filter(a => !comp || a.business_name === comp);
}}
function allPromos() {{ // for charts (city-filtered only, not comp)
  const city = getCity();
  return PROMOS.filter(p => !city || (p.city||'').trim() === city);
}}

// ─── Dropdown population ────────────────────────────────────────────────────
function populateCompDropdown() {{
  const all = [...new Set([...PROMOS.map(p=>p.business_name),...ADS.map(a=>a.business_name)])].sort();
  const sel = document.getElementById('compFilter');
  all.forEach(n => {{ const o=document.createElement('option'); o.value=o.textContent=n; sel.appendChild(o); }});
}}

// ─── Chart selection ────────────────────────────────────────────────────────
function selectComp(name) {{
  _chartSel = _chartSel === name ? null : name;
  const chip = document.getElementById('activeChip');
  if (_chartSel) {{
    document.getElementById('chipLabel').textContent = _chartSel;
    chip.style.display = 'inline-flex';
    if (window.Motion) Motion.animate('#activeChip', {{ opacity:[0,1], scale:[0.9,1] }}, {{ duration:0.2 }});
  }} else {{ chip.style.display = 'none'; }}
  renderAll();
}}
function clearChartSel() {{
  _chartSel = null;
  document.getElementById('activeChip').style.display='none';
  renderAll();
}}
function applyFilters() {{
  _chartSel = null;
  document.getElementById('activeChip').style.display='none';
  renderAll();
}}

// ─── Chart helpers ──────────────────────────────────────────────────────────
const _charts = {{}};
function mkChart(id, type, data, extra={{}}) {{
  if (_charts[id]) _charts[id].destroy();
  _charts[id] = new Chart(document.getElementById(id).getContext('2d'), {{
    type, data,
    options: {{
      responsive:true,
      animation:{{ duration:500, easing:'easeInOutCubic' }},
      interaction:{{ mode:'index', intersect:false }},
      plugins:{{
        legend: extra.hideLegend ? {{display:false}} : {{
          position: extra.legendPos||'bottom',
          labels:{{ color:'#64748b', font:{{size:11,family:'Inter'}}, boxWidth:10, padding:14, usePointStyle:true }}
        }},
        tooltip:{{ enabled:false, external: makeTooltip }},
      }},
      scales: extra.noScales ? {{x:{{display:false}},y:{{display:false}}}} : {{
        x:{{ ticks:{{color:'#94a3b8',font:{{size:11,family:'Inter'}}}}, grid:{{color:'#f1f5f9'}}, border:{{display:false}} }},
        y:{{ ticks:{{color:'#94a3b8',font:{{size:11,family:'Inter'}},maxTicksLimit:6}}, grid:{{color:'#f1f5f9'}}, border:{{display:false}} }},
        ...(extra.stacked ? {{x:{{stacked:true}},y:{{stacked:true}}}} : {{}}),
      }},
      onHover:(e,els) => {{ e.native.target.style.cursor = els.length?'pointer':'default'; }},
      onClick:(e,els,ch) => {{
        if (!els.length) return;
        const label = ch.data.labels[els[0].index];
        if (label) selectComp(label);
      }},
      ...( extra.indexAxis ? {{indexAxis:extra.indexAxis}} : {{}} ),
    }}
  }});
}}

// ─── External tooltip ───────────────────────────────────────────────────────
function makeTooltip(context) {{
  const el = document.getElementById('chartTooltip');
  if (context.tooltip.opacity === 0) {{ el.style.opacity='0'; return; }}
  const t = context.tooltip;
  let html = `<div class="tt-title">${{t.title[0]||''}}</div>`;
  t.dataPoints.forEach(dp => {{
    html += `<div class="tt-row">
      <span style="width:8px;height:8px;border-radius:50%;background:${{dp.dataset.borderColor}};display:inline-block"></span>
      ${{dp.dataset.label||''}}
      <span class="tt-val">${{dp.formattedValue}}</span>
    </div>`;
  }});
  el.innerHTML = html;
  const rect = context.chart.canvas.getBoundingClientRect();
  el.style.left = (rect.left + t.caretX + 12) + 'px';
  el.style.top  = (rect.top  + t.caretY - 20) + 'px';
  el.style.opacity = '1';
}}
document.addEventListener('mouseleave', () => {{
  const el = document.getElementById('chartTooltip');
  if (el) el.style.opacity = '0';
}}, true);

// ─── Bar colors (dim unselected when something is selected) ─────────────────
function barColors(labels, alpha='cc') {{
  return labels.map(l => {{
    const base = colorFor(l);
    const dim  = !_chartSel || l === _chartSel;
    return base + (dim ? alpha : '30');
  }});
}}

// ─── Count-up animation ──────────────────────────────────────────────────────
function countUp(el, target, suffix='') {{
  const start = performance.now(), dur = 700;
  const from = parseInt(el.textContent) || 0;
  function tick(now) {{
    const p = Math.min((now-start)/dur, 1);
    const ease = 1-Math.pow(1-p,3);
    el.textContent = Math.round(from + (target-from)*ease) + suffix;
    if (p < 1) requestAnimationFrame(tick);
  }}
  requestAnimationFrame(tick);
}}

// ─── KPIs ────────────────────────────────────────────────────────────────────
function renderKPIs() {{
  const p = filteredPromos(), a = filteredAds();
  const wDisc = p.filter(x=>x.discount_value&&x.discount_value.trim()).length;
  const pct = p.length ? Math.round(wDisc/p.length*100) : 0;
  const comps = new Set([...p.map(x=>x.business_name),...a.map(x=>x.business_name)]).size;
  const kpis = [
    {{val:p.length, lbl:'Promotions',    sub:'active deals',       color:'#6366f1'}},
    {{val:a.length, lbl:'Google Ads',    sub:'text creatives',     color:'#059669'}},
    {{val:comps,    lbl:'Competitors',   sub:'tracked',            color:'#d97706'}},
    {{val:3,        lbl:'Cities',        sub:'Edmonton · Calgary · GP', color:'#2563eb'}},
    {{val:wDisc,    lbl:'w/ Discount',   sub:pct+'% of promos',    color:'#db2777'}},
  ];
  const row = document.getElementById('kpiRow');
  row.innerHTML = kpis.map((k,i)=>`
    <div class="kpi" style="--kpi-color:${{k.color}}">
      <div class="val" id="kpi${{i}}">0</div>
      <div class="lbl">${{k.lbl}}</div>
      <div class="sub">${{k.sub}}</div>
    </div>`).join('');
  kpis.forEach((k,i) => countUp(document.getElementById('kpi'+i), k.val));
}}

// ─── Spotlight ───────────────────────────────────────────────────────────────
function discScore(val) {{
  if (!val) return -1;
  const v = String(val).toLowerCase().trim();
  if (v==='free'||v.startsWith('free')) return 9999;
  if (v.includes('%')) return (parseFloat(v)||0) * 8;
  return parseFloat(v.replace(/[^0-9.]/g,''))||0;
}}
function renderSpotlight() {{
  const promos = filteredPromos();
  const top3 = [...promos].sort((a,b)=>discScore(b.discount_value)-discScore(a.discount_value)).filter(p=>p.discount_value&&p.discount_value.trim()).slice(0,3);
  document.getElementById('spotlightCount').textContent = top3.length + ' shown';
  document.getElementById('spotlightRow').innerHTML = top3.map((p,i)=>`
    <div class="deal-card">
      <div class="deal-rank">${{i+1}}</div>
      <div class="disc-badge" style="color:${{colorFor(p.business_name)}}">${{p.discount_value}}</div>
      <div class="comp"><span class="comp-dot" style="background:${{colorFor(p.business_name)}}"></span>${{p.business_name}}</div>
      <div class="svc">${{p.service_name||p.category||''}}</div>
      <div class="desc">${{(p.offer_details||p.promo_description||'').slice(0,100)}}${{(p.offer_details||p.promo_description||'').length>100?'…':''}}</div>
    </div>`).join('') || '<p class="empty">No discounts in current filter.</p>';
}}

// ─── Charts ──────────────────────────────────────────────────────────────────
function renderCharts() {{
  const prAll = allPromos();
  const countBy = (arr,k) => {{ const m={{}}; arr.forEach(r=>{{ const v=r[k]||'?'; m[v]=(m[v]||0)+1; }}); return m; }};
  const sorted = obj => Object.entries(obj).sort((a,b)=>b[1]-a[1]);

  // Promos per competitor
  const pe = sorted(countBy(prAll,'business_name'));
  mkChart('promosBar','bar',{{
    labels: pe.map(e=>e[0]),
    datasets:[{{ label:'Promotions', data:pe.map(e=>e[1]),
      backgroundColor:barColors(pe.map(e=>e[0]),'bb'),
      borderColor:pe.map(e=>colorFor(e[0])),
      borderWidth:1.5, borderRadius:5, borderSkipped:false }}]
  }},{{ indexAxis:'y', hideLegend:true }});

  // Ads per competitor
  const ae = sorted(countBy(ADS,'business_name'));
  mkChart('adsBar','bar',{{
    labels: ae.map(e=>e[0]),
    datasets:[{{ label:'Ads', data:ae.map(e=>e[1]),
      backgroundColor:barColors(ae.map(e=>e[0]),'bb'),
      borderColor:ae.map(e=>colorFor(e[0])),
      borderWidth:1.5, borderRadius:5, borderSkipped:false }}]
  }},{{ indexAxis:'y', hideLegend:true }});

  // Category donut
  const ce = sorted(countBy(prAll,'category'));
  mkChart('catDonut','doughnut',{{
    labels: ce.map(e=>e[0]),
    datasets:[{{ data:ce.map(e=>e[1]),
      backgroundColor:ce.map((_,i)=>PALETTE[i%PALETTE.length]+'cc'),
      borderColor:ce.map((_,i)=>PALETTE[i%PALETTE.length]),
      borderWidth:1.5, hoverOffset:6 }}]
  }},{{ noScales:true, legendPos:'bottom' }});

  // City donut
  const cityMap={{}};
  prAll.forEach(p=>{{ const c=(p.city||'Unknown').trim(); cityMap[c]=(cityMap[c]||0)+1; }});
  const cityPalette=['#6366f1','#d97706','#059669'];
  const cityEntries = Object.entries(cityMap).sort((a,b)=>b[1]-a[1]);
  mkChart('cityDonut','doughnut',{{
    labels: cityEntries.map(e=>e[0]),
    datasets:[{{ data:cityEntries.map(e=>e[1]),
      backgroundColor:cityPalette.map(c=>c+'cc'),
      borderColor:cityPalette,
      borderWidth:1.5, hoverOffset:6 }}]
  }},{{ noScales:true, legendPos:'bottom' }});

  // Discount buckets
  const buckets={{}};
  prAll.forEach(p=>{{ const v=(p.discount_value||'').toLowerCase().trim();
    let b;
    if (!v) return;
    if (v==='free'||v.startsWith('free')) b='Free';
    else if (v.includes('%')) b='%';
    else {{ const n=parseFloat(v.replace(/[^0-9.]/g,''));
      if(isNaN(n)) b='Other';
      else if(n<=10) b='≤$10';
      else if(n<=25) b='$11–25';
      else if(n<=50) b='$26–50';
      else b='$50+';
    }}
    buckets[b]=(buckets[b]||0)+1;
  }});
  const bOrder=['Free','%','≤$10','$11–25','$26–50','$50+'];
  const bCols=['#059669','#6366f1','#d97706','#ea580c','#db2777','#dc2626'];
  mkChart('discBar','bar',{{
    labels:bOrder,
    datasets:[{{ label:'Promos', data:bOrder.map(k=>buckets[k]||0),
      backgroundColor:bCols.map(c=>c+'bb'),
      borderColor:bCols, borderWidth:1.5, borderRadius:5, borderSkipped:false }}]
  }},{{ hideLegend:true }});

  // Stacked ads
  const discC={{}}, noC={{}};
  ADS.forEach(a=>{{ const n=a.business_name;
    if(a.discount_value&&a.discount_value.trim()) discC[n]=(discC[n]||0)+1;
    else noC[n]=(noC[n]||0)+1;
  }});
  const adComps=[...new Set(ADS.map(a=>a.business_name))].sort((a,b)=>((discC[b]||0)+(noC[b]||0))-((discC[a]||0)+(noC[a]||0)));
  mkChart('stackedBar','bar',{{
    labels:adComps,
    datasets:[
      {{ label:'With Discount', data:adComps.map(n=>discC[n]||0), backgroundColor:'#05966999', borderColor:'#059669', borderWidth:1.5, borderRadius:4, borderSkipped:false }},
      {{ label:'No Discount',   data:adComps.map(n=>noC[n]||0),  backgroundColor:'#64748b33', borderColor:'#94a3b8',  borderWidth:1.5, borderRadius:4, borderSkipped:false }},
    ]
  }},{{ stacked:true }});

  // Discount ads only
  const discAds = ADS.filter(a=>a.discount_value&&a.discount_value.trim());
  const dae = sorted(countBy(discAds,'business_name'));
  mkChart('discAdsBar','bar',{{
    labels:dae.map(e=>e[0]),
    datasets:[{{ label:'Discount Ads', data:dae.map(e=>e[1]),
      backgroundColor:dae.map(e=>colorFor(e[0])+'bb'),
      borderColor:dae.map(e=>colorFor(e[0])),
      borderWidth:1.5, borderRadius:5, borderSkipped:false }}]
  }},{{ indexAxis:'y', hideLegend:true }});
}}

// ─── Tables ──────────────────────────────────────────────────────────────────
const CAT_CLS = {{ 'Oil Change':'b-green','Tire Sales':'b-blue','Battery':'b-amber','Brake':'b-red','Other':'b-gray' }};

function renderTable() {{
  const q = (document.getElementById('promoSearch').value||'').toLowerCase();
  const sort = _sortState.promos;
  let rows = filteredPromos().filter(p => !q ||
    [p.business_name,p.service_name,p.offer_details,p.promo_description,p.discount_value,p.category]
    .join(' ').toLowerCase().includes(q)
  );
  if (sort.key) rows.sort((a,b)=>((a[sort.key]||'')>(b[sort.key]||'')?1:-1)*sort.dir);
  document.getElementById('promoCount').textContent = rows.length + ' records';
  const tbody = document.getElementById('promoTbody');
  tbody.innerHTML = rows.map(p=>{{
    const cat = p.category||'';
    const disc = p.discount_value||'';
    return `<tr>
      <td><div class="comp-cell"><span class="comp-dot" style="background:${{colorFor(p.business_name)}}"></span>${{p.business_name}}</div></td>
      <td>${{(p.city||'').trim()}}</td>
      <td>${{p.service_name||''}}</td>
      <td>${{disc?`<span class="badge b-green">${{disc}}</span>`:'<span style="color:#cbd5e1">—</span>'}}</td>
      <td style="color:var(--muted);max-width:240px">${{(p.offer_details||p.promo_description||'').slice(0,100)}}</td>
      <td style="color:var(--muted);white-space:nowrap">${{p.expiry_date||'—'}}</td>
      <td>${{cat?`<span class="badge ${{CAT_CLS[cat]||'b-gray'}}">${{cat}}</span>`:''}}</td>
    </tr>`;
  }}).join('') || `<tr><td colspan="7" class="empty">No results.</td></tr>`;
}}

function renderAdsTable() {{
  const q = (document.getElementById('adsSearch').value||'').toLowerCase();
  const sort = _sortState.ads;
  let rows = filteredAds().filter(a => !q ||
    [a.business_name,a.ad_title,a.ad_description,a.discount_value,a.displayed_link].join(' ').toLowerCase().includes(q)
  );
  if (sort.key) rows.sort((a,b)=>((a[sort.key]||'')>(b[sort.key]||'')?1:-1)*sort.dir);
  document.getElementById('adsCount').textContent = rows.length + ' creatives';
  const tbody = document.getElementById('adsTbody');
  tbody.innerHTML = rows.map(a=>{{
    const disc = a.discount_value||'';
    return `<tr>
      <td><div class="comp-cell"><span class="comp-dot" style="background:${{colorFor(a.business_name)}}"></span>${{a.business_name}}</div></td>
      <td style="font-weight:500;max-width:200px">${{a.ad_title||''}}</td>
      <td style="color:var(--muted);max-width:300px">${{(a.ad_description||'').slice(0,110)}}</td>
      <td>${{disc?`<span class="badge b-amber">${{disc}}</span>`:'<span style="color:#cbd5e1">—</span>'}}</td>
      <td><span style="color:var(--accent);font-size:11.5px">${{a.displayed_link||''}}</span></td>
    </tr>`;
  }}).join('') || `<tr><td colspan="5" class="empty">No results.</td></tr>`;
}}

// ─── Sort ────────────────────────────────────────────────────────────────────
function sortTable(which, key, th) {{
  const s = _sortState[which];
  s.dir = (s.key===key) ? -s.dir : 1;
  s.key = key;
  document.querySelectorAll('thead th').forEach(t=>t.className='');
  th.className = s.dir===1 ? 'sorted-asc' : 'sorted-desc';
  which==='promos' ? renderTable() : renderAdsTable();
}}

// ─── Master refresh ──────────────────────────────────────────────────────────
function renderAll() {{
  renderKPIs();
  renderSpotlight();
  renderCharts();
  renderTable();
  renderAdsTable();
}}

// ─── Animations ──────────────────────────────────────────────────────────────
function initAnimations() {{
  if (typeof Motion !== 'undefined') {{
    const {{ animate, spring }} = Motion;
    // All cards and KPIs animate in at once (no stagger)
    animate(
      document.querySelectorAll('.card, .kpi, .deal-card'),
      {{ opacity:[0,1], transform:['translateY(20px)','translateY(0px)'] }},
      {{ duration:0.5, easing:spring({{ stiffness:140, damping:20 }}) }}
    );
  }} else {{
    document.querySelectorAll('.card,.kpi,.deal-card').forEach(el=>{{
      el.style.opacity='1'; el.style.transform='none';
    }});
  }}
}}

// ─── Boot ────────────────────────────────────────────────────────────────────
populateCompDropdown();
renderAll();
// Defer animations until after first paint
requestAnimationFrame(() => setTimeout(initAnimations, 50));
</script>
</body>
</html>"""


def main():
    p = argparse.ArgumentParser(description="Generate competitor intelligence dashboard")
    p.add_argument("--open", action="store_true", help="Open in browser after generating.")
    args = p.parse_args()

    ads, promos = load_data()
    print(f"Loaded {len(ads)} ads, {len(promos)} promotions")

    html = build_html(ads, promos)
    OUT_FILE.write_text(html, encoding="utf-8")
    print(f"Dashboard → {OUT_FILE}")

    if args.open:
        webbrowser.open(OUT_FILE.as_uri())


if __name__ == "__main__":
    main()
