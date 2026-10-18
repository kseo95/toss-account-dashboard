// Agent 답변의 그래프(charts). 서버(charts.py)가 숫자·짧은 글만 담은 명세를 주면 Chart.js로 그린다. 글은 canvas에 그려지므로 HTML로 해석되지 않는다.
// 명세: {type: 'bar'|'hbar'|'line', title, subtitle, caption, unit, labels: [...], datasets: [{label, data: [...], color|colors: [토큰...], text: [점마다 보일 글...]}],
//        markers: [{index, text, color}] (line), hlines: [{value, text, color}] (line), height}
// 색 토큰: up(오름·초록) down(내림·빨강) neutral accent warn s1~s4 — 이 앱의 손익 색(--profit/--danger)을 따른다.
const AGENT_CHART_COLORS = {
  up: '#4fae5f', down: '#e66767', neutral: '#8b949e', accent: '#3b82f6', warn: '#d29922',
  s1: '#3b82f6', s2: '#8b949e', s3: '#d29922', s4: '#a371f7',
};
const AGENT_CHART_TEXT = '#e6edf3';
const AGENT_CHART_MUTED = '#8b949e';
const AGENT_CHART_GRID = '#30363d';
let agentChartInstances = [];

function agentChartColor(token) {
  return AGENT_CHART_COLORS[token] || AGENT_CHART_COLORS.neutral;
}

function agentChartNumber(v) {
  return typeof v === 'number' && isFinite(v) ? v : null;
}

// signed=false(가격선)는 + 기호를 붙이지 않는다. 축 눈금(axis=true)은 정수면 소수점을 떼고 천 단위로 끊는다.
function agentChartFormat(value, unit, signed = true, axis = false, decimals = null) {
  if (value === null || value === undefined) return '';
  if (unit === '원') return Math.round(value).toLocaleString() + '원';
  const digits = decimals !== null ? decimals : (axis && Number.isInteger(value) ? 0 : (Math.abs(value) >= 100 ? 0 : 1));
  const body = Math.abs(value) >= 1000 ? value.toLocaleString(undefined, { maximumFractionDigits: digits }) : value.toFixed(digits);
  return (signed && value > 0 ? '+' : '') + body + (unit || '');
}

// 선 그래프에서 점·가로선에 붙는 설명(글)을 직접 그린다.
const agentChartExtras = {
  id: 'agentExtras',
  afterDatasetsDraw(chart, _args, opts) {
    const { ctx, chartArea, scales } = chart;
    if (!opts || !scales.x || !scales.y) return;
    ctx.save();
    ctx.font = '11px sans-serif';
    (opts.hlines || []).forEach(h => {
      const y = scales.y.getPixelForValue(h.value);
      if (!isFinite(y) || y < chartArea.top || y > chartArea.bottom) return;
      ctx.strokeStyle = agentChartColor(h.color || 'neutral');
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(chartArea.left, y); ctx.lineTo(chartArea.right, y); ctx.stroke();
      ctx.setLineDash([]);
      if (h.text) { ctx.fillStyle = agentChartColor(h.color || 'neutral'); ctx.textAlign = 'left'; ctx.fillText(h.text, chartArea.left + 4, y - 4); }
    });
    (opts.markers || []).forEach(m => {
      const meta = chart.getDatasetMeta(0);
      const point = meta && meta.data[m.index];
      if (!point) return;
      ctx.fillStyle = agentChartColor(m.color || 'down');
      ctx.beginPath(); ctx.arc(point.x, point.y, 4.5, 0, Math.PI * 2); ctx.fill();
      if (m.text) {
        const w = ctx.measureText(m.text).width;
        const x = Math.min(Math.max(point.x, chartArea.left + w / 2), chartArea.right - w / 2);
        const below = point.y < (chartArea.top + chartArea.bottom) / 2;
        ctx.fillStyle = AGENT_CHART_TEXT; ctx.textAlign = 'center';
        ctx.fillText(m.text, x, below ? point.y + 18 : point.y - 9);
      }
    });
    ctx.restore();
  },
};

function agentChartConfig(spec) {
  const horizontal = spec.type === 'hbar';
  const isLine = spec.type === 'line';
  const unit = spec.unit || '';
  const signed = spec.signed !== undefined ? !!spec.signed : !isLine;
  const decimals = Number.isInteger(spec.decimals) ? spec.decimals : null;
  const datasets = (spec.datasets || []).map((d, di) => {
    const data = (d.data || []).map(agentChartNumber);
    const colors = (d.colors && d.colors.length ? d.colors : data.map(() => d.color || 's' + ((di % 4) + 1))).map(agentChartColor);
    const texts = d.text || [];
    const base = { label: d.label || '', data };
    if (isLine) {
      return { ...base, borderColor: colors[0], backgroundColor: colors[0], borderWidth: 2, pointRadius: 0, pointHoverRadius: 3, tension: 0.15, fill: false,
               datalabels: { display: false } };
    }
    return { ...base, backgroundColor: colors, borderRadius: 3, maxBarThickness: horizontal ? 22 : 44,
             datalabels: {
               display: ctx => ctx.dataset.data[ctx.dataIndex] !== null,
               color: AGENT_CHART_TEXT, clamp: true, font: { size: 11 },
               anchor: ctx => (horizontal && ctx.dataset.data[ctx.dataIndex] < 0 && String(texts[ctx.dataIndex] || '').length > 14) ? 'start' : 'end',
               align: ctx => (horizontal && ctx.dataset.data[ctx.dataIndex] < 0 && String(texts[ctx.dataIndex] || '').length > 14) ? 'right' : 'end',
               formatter: (value, ctx) => String(texts[ctx.dataIndex] || agentChartFormat(value, unit, signed, false, decimals)).split('\n'),
             } };
  });
  const multi = datasets.length > 1;
  const hasLongText = (spec.datasets || []).some(d => (d.text || []).some(t => String(t).length > 14));
  const scaleStyle = { ticks: { color: AGENT_CHART_MUTED, font: { size: 11 } }, grid: { color: AGENT_CHART_GRID } };
  const valueScale = { ...scaleStyle, ticks: { ...scaleStyle.ticks, callback: v => agentChartFormat(v, unit, signed, true, decimals) }, grace: isLine ? '5%' : '18%', ...(typeof spec.ymin === 'number' ? { min: spec.ymin } : {}), ...(typeof spec.ymax === 'number' ? { max: spec.ymax } : {}) };
  const categoryScale = { ...scaleStyle, grid: { display: false }, ticks: { ...scaleStyle.ticks, autoSkip: true, maxTicksLimit: isLine ? 8 : 40, maxRotation: 0 } };
  return {
    type: isLine ? 'line' : 'bar',
    data: { labels: spec.labels || [], datasets },
    options: {
      indexAxis: horizontal ? 'y' : 'x', responsive: true, maintainAspectRatio: false, animation: false,
      interaction: isLine ? { mode: 'index', intersect: false } : undefined,
      layout: { padding: { top: isLine ? 16 : 20, right: horizontal ? (hasLongText ? 150 : 56) : 8 } },
      scales: horizontal ? { x: valueScale, y: categoryScale } : { x: categoryScale, y: valueScale },
      plugins: {
        legend: { display: multi, labels: { color: AGENT_CHART_MUTED, boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => (c.dataset.label ? c.dataset.label + ': ' : '') + agentChartFormat(c.parsed[horizontal ? 'x' : 'y'], unit, signed, false, decimals) } },
        agentExtras: isLine ? { markers: spec.markers || [], hlines: spec.hlines || [] } : undefined,
      },
    },
    plugins: isLine ? [agentChartExtras] : [ChartDataLabels],
  };
}

function renderAgentCharts(container, charts) {
  agentChartInstances.forEach(c => c.destroy());
  agentChartInstances = [];
  container.innerHTML = '';
  if (typeof Chart === 'undefined') return;
  (charts || []).forEach(spec => {
    if (!spec || !Array.isArray(spec.labels) || !Array.isArray(spec.datasets)) return;
    const card = document.createElement('div');
    card.className = 'agent-chart';
    const head = document.createElement('div');
    head.className = 'agent-chart-head';
    const title = document.createElement('strong'); title.textContent = String(spec.title || '');
    head.appendChild(title);
    if (spec.subtitle) { const sub = document.createElement('span'); sub.className = 'muted'; sub.textContent = ' ' + String(spec.subtitle); head.appendChild(sub); }
    card.appendChild(head);
    const wrap = document.createElement('div');
    const rows = (spec.labels || []).length;
    const height = Number(spec.height) || (spec.type === 'hbar' ? Math.max(140, rows * 30 + 56) : 260);
    wrap.style.height = Math.min(height, 560) + 'px';
    wrap.style.position = 'relative';
    const canvas = document.createElement('canvas');
    wrap.appendChild(canvas);
    card.appendChild(wrap);
    if (spec.caption) { const cap = document.createElement('div'); cap.className = 'muted agent-chart-caption'; cap.textContent = String(spec.caption); card.appendChild(cap); }
    container.appendChild(card);
    agentChartInstances.push(new Chart(canvas, agentChartConfig(spec)));
  });
}
