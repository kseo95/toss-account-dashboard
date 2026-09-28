// 주간 리포트: 표·요약·자동 코멘트 렌더링과 구성 설정(그룹·종목·MA 기간) 편집.
// 계산은 전부 서버(weekly_report.py)가 하고, 여기서는 받은 숫자를 표로 그리기만 한다.

const WR_GROUP_TYPE_LABELS = {
  basic: '기본',
  sector: '비교 기준 대비 포함',
  dividend: '배당주',
  risk: '위험 신호',
  fx_account: '원화 계좌 환산 (주가지수 + 환율 2개)',
  my_account: '내 계좌 (토스 보유종목 자동)',
};
const WR_ITEM_KIND_LABELS = {
  price: '가격',
  rate: '금리 (%p 변화)',
  level: '환율·공포지수 (52주 위치)',
};

function wrNum(v) {
  return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function wrPctCell(v, suffix = '%') {
  if (v === null || v === undefined) return '<td>–</td>';
  const sign = v > 0 ? '+' : '';
  return `<td class="${signClass(v)}">${sign}${v.toFixed(2)}${suffix}</td>`;
}
function wrWeekCell(r) {
  return r.kind === 'rate' ? wrPctCell(r.change_pt, '%p') : wrPctCell(r.change_pct);
}
function wrDrawdownCell(r) {
  if (r.kind !== 'price') {
    return `<td>${r.position_52w_pct === null ? '–' : '위치 ' + Math.round(r.position_52w_pct) + '%'}</td>`;
  }
  return `<td>${r.drawdown_52w_pct > -0.05 ? '신고가' : r.drawdown_52w_pct.toFixed(1) + '%'}</td>`;
}
function wrNearestCell(r) {
  if (!r.nearest) return `<td>MA 없음 (${r.weeks}주)</td>`;
  const d = r.nearest.dist_pct;
  return `<td>${r.nearest.period}주 (${d > 0 ? '+' : ''}${d.toFixed(1)}%)</td>`;
}
function wrCrossCell(r) {
  const parts = r.mas.filter(m => m.cross).map(m =>
    `<span class="wr-cross ${m.cross}">${m.cross === 'up' ? '▲' : '▼'}${m.period}</span>`);
  return `<td>${parts.join('')}</td>`;
}
function wrSlopeCell(r) {
  if (r.slope_pct === null) return '<td>–</td>';
  return `<td>${r.slope_pct > 0.3 ? '↑' : (r.slope_pct < -0.3 ? '↓' : '→')}</td>`;
}
function wrOrderCells(r) {
  if (!r.mas.length) return '<td>–</td><td>–</td>';
  // 정배열/역배열이면 순서를 다 쓰지 않고 이름만 (표 폭 절약). 섞여 있을 때만 순서를 보여준다.
  const orderText = r.ma_alignment === 'up' ? '정배열' : (r.ma_alignment === 'down' ? '역배열' : r.ma_order.join('>'));
  const n = r.broken.length, total = r.mas.length;
  // 가격 종목만 색칠: 공포지수·금리·환율은 "선 아래 = 나쁨"이 아니라서 색으로 판단하게 만들지 않는다.
  let cls = '';
  if (r.kind === 'price') cls = n === total ? 'broken-all' : (n > 0 ? 'broken-some' : '');
  const detail = n > 0 && n < total ? ` (${r.broken.join(', ')})` : '';
  return `<td>${orderText}</td><td class="${cls}"><b>${n}/${total}</b>${detail}</td>`;
}

function wrColumns(type, report) {
  const bench = report.benchmark.symbol;
  const slopeTitle = `${report.ma_periods[0]}주선 기울기`;
  const tail = ['52주 고점 대비', '최근접 주봉 MA', '크로스', slopeTitle, 'MA 순서 (1→4위)', '하향돌파'];
  if (type === 'sector') return ['주간', `${bench} 대비`, ...tail];
  if (type === 'my_account') return ['비중', '주간 (현지)', '주간 (원화)', '기여도', ...tail];
  if (type === 'dividend') return ['주간', '총수익 주간', '배당률 (연 배당)', `${report.rate.symbol} 대비`, ...tail];
  return ['현재', '주간', ...tail];
}
function wrRowCells(type, r) {
  const tail = wrDrawdownCell(r) + wrNearestCell(r) + wrCrossCell(r) + wrSlopeCell(r) + wrOrderCells(r);
  if (type === 'my_account') {
    const head = `<td>${r.weight_pct.toFixed(1)}%</td>`;
    const krw = wrPctCell(r.krw_change_pct) + wrPctCell(r.contribution_pctp, '%p');
    // 예수금 줄은 시세가 없어 비중·원화 주간·기여도만
    if (r.kind === 'cash') return head + '<td>–</td>' + krw + '<td colspan="6"></td>';
    return head + wrPctCell(r.total_return_pct) + krw + tail;
  }
  if (type === 'sector') return wrWeekCell(r) + wrPctCell(r.rel_pct, '%p') + tail;
  if (type === 'dividend') {
    const yld = r.dividend_yield_pct === null ? '<td>–</td>'
      : `<td>${r.dividend_yield_pct.toFixed(2)}% (${wrNum(r.dividend_ttm)})</td>`;
    return wrWeekCell(r) + wrPctCell(r.total_return_pct) + yld + wrPctCell(r.spread_pct, '%p') + tail;
  }
  const cur = r.kind === 'rate' ? r.close.toFixed(2) + '%' : wrNum(r.close);
  return `<td>${cur}</td>` + wrWeekCell(r) + tail;
}

function renderWeeklyReport(report) {
  const from = report.prev_week_end || '?';
  $('wrPeriod').innerHTML =
    `${from} → ${report.week_end}<span class="sub">주별 마지막 거래일 종가 기준 · ${esc(report.generated_at)} 계산</span>`;

  const s = report.summary;
  const broken = s.all_broken.length
    ? s.all_broken.map(x => esc(x.label)).join(', ') : '<span class="none">없음</span>';
  const crosses = s.crosses.length
    ? s.crosses.map(c => `<span class="wr-cross ${c.dir}">${c.dir === 'up' ? '▲' : '▼'}</span>${esc(c.label)} ${c.period}주`).join(' · ')
    : '<span class="none">없음</span>';
  const failed = Object.keys(report.errors);
  $('wrSummary').innerHTML =
    `<div><span class="label">모든 주봉 MA 아래 (가격)</span>${broken}</div>` +
    `<div><span class="label">이번 주 MA 크로스</span>${crosses}</div>` +
    ((s.comments || []).length ? `<ul class="wr-comments">${s.comments.map(c => `<li>${esc(c)}</li>`).join('')}</ul>` : '') +
    (failed.length ? `<div><span class="label">데이터 못 받은 심볼</span><span class="loss">${failed.map(esc).join(', ')}</span></div>` : '');

  const p = report.ma_periods;
  $('wrLegend').innerHTML = [
    '<b>52주 고점 대비</b>: 최근 1년 최고 종가 대비. 금리·환율·공포지수는 대신 <b>52주 위치</b>(0% = 1년 최저, 100% = 1년 최고).',
    '<b>최근접 주봉 MA</b>: 설정된 주봉 MA 중 현재가와 가장 가까운 선과 거리.',
    '<b>크로스</b>: 이번 주에 새로 뚫은 선. ▲ 아래→위 돌파, ▼ 위→아래 이탈.',
    `<b>${p[0]}주선 기울기</b>: ${p[0]}주 MA의 최근 4주 변화 (±0.3% 이내는 →).`,
    `<b>MA 순서</b>: 차트 위→아래 순서. ${p.join('>')}이면 정배열(상승 추세), 반대면 역배열(하락 추세).`,
    `<b>하향돌파</b>: 현재가 위에 있는 MA 개수 = 위에서부터 몇 단계를 깨고 내려왔는지. 0/${p.length} 모든 선 위, ${p.length}/${p.length} 모든 선 아래.`,
    '공포지수(VIX·MOVE 등)는 해석이 반대 — 모든 선 아래 = 평온, 선을 위로 뚫는 게 경고. 그래서 가격 종목만 색으로 표시합니다.',
  ].join('<br>');

  const wrap = $('wrGroups');
  wrap.innerHTML = '';
  report.groups.forEach((g, i) => {
    const cols = wrColumns(g.type, report);
    const rows = g.rows.map(r => {
      const stale = r.stale ? `<span class="stale">마지막 거래 ${esc(r.last_date)}</span>` : '';
      const cashTag = r.is_cash && r.kind !== 'cash' ? '<span class="sym">현금 취급</span>' : '';
      const name = `<td class="nm">${esc(r.label)}<span class="sym">${esc(r.symbol)}</span>${cashTag}${stale}</td>`;
      if (r.error) return `<tr>${name}<td class="err" colspan="${cols.length}">데이터 없음: ${esc(r.error)}</td></tr>`;
      return `<tr>${name}${wrRowCells(g.type, r)}</tr>`;
    }).join('');
    let extra = '';
    if (g.type === 'sector') extra = `<div class="wr-note">${esc(report.benchmark.symbol)} 대비 = 주간 수익률 − ${esc(report.benchmark.symbol)} 주간 수익률(${report.benchmark.change_pct === null ? '–' : fmtPct(report.benchmark.change_pct)})</div>`;
    if (g.type === 'dividend') extra = `<div class="wr-note">연 배당 = 최근 12개월 실지급 합계(TTM). ${esc(report.rate.symbol)} 대비 = 배당률 − ${esc(report.rate.symbol)} (${report.rate.value === null ? '–' : report.rate.value.toFixed(2) + '%'}). 한국 ETF 분배금은 Yahoo에서 중복 집계될 수 있음.</div>`;
    if (g.type === 'fx_account' && g.account_pct !== undefined) {
      const a = g.rows[0].change_pct, b = g.rows[1].change_pct;
      extra = `<div class="wr-account">원화 환산 주간 ≈ <b class="${signClass(g.account_pct)}">${fmtPct(g.account_pct)}</b>
        <span class="wr-note">= (1 ${fmtPct(a)}) × (1 ${fmtPct(b)}) − 1</span></div>`;
    }
    if (g.type === 'my_account') {
      if (g.error) extra = `<div class="wr-status error">${esc(g.error)}</div>`;
      else if (g.account) {
        const a = g.account;
        const wk = a.week_krw_pct === null ? '–' : `<b class="${signClass(a.week_krw_pct)}">${fmtPct(a.week_krw_pct)}</b>`;
        extra = `<div class="wr-account">총 평가 <b>${fmtWon(a.eval_krw)}</b> · 누적 <span class="${signClass(a.rate_pct)}">${fmtPct(a.rate_pct)}</span> (${fmtWon(a.profit_loss_krw)}) · 이번 주 원화 기준 ≈ ${wk}</div>
          <div class="wr-note">원화 주간 = 해외는 (1 + 배당 포함 주간수익) × (1 + ${esc(report.fx.symbol)} ${report.fx.change_pct === null ? '–' : fmtPct(report.fx.change_pct)}) − 1. 기여도 = 현재 비중 × 원화 주간 — 한 주 동안 보유 수량이 그대로였다고 가정한 근사치.</div>`;
      }
    }
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `<h2>${i + 1}. ${esc(g.title)}</h2>
      <div class="wr-scroll"><table class="wr-table"><thead><tr><th>종목</th>${cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>
      <tbody>${rows || `<tr class="empty-row"><td colspan="${cols.length + 1}">종목이 없습니다.</td></tr>`}</tbody></table></div>${extra}
      ${(g.comments || []).length ? `<ul class="wr-comments">${g.comments.map(c => `<li>${esc(c)}</li>`).join('')}</ul>` : ''}`;
    wrap.appendChild(card);
  });
}

async function loadWeeklyReport(refresh) {
  const status = $('wrStatus');
  const btn = $('wrRefreshBtn');
  status.className = 'wr-status';
  status.textContent = '주간 리포트 계산 중... (종목이 많아 몇 초 걸립니다)';
  status.style.display = 'block';
  btn.disabled = true;
  try {
    const res = await fetch('/api/weekly-report' + (refresh ? '?refresh=1' : ''));
    const data = await res.json();
    if (!res.ok) {
      status.className = 'wr-status error';
      status.textContent = data.error || '주간 리포트를 불러오지 못했습니다.';
      return;
    }
    status.style.display = 'none';
    renderWeeklyReport(data);
  } catch (e) {
    status.className = 'wr-status error';
    status.textContent = '서버에 연결할 수 없습니다.';
  } finally {
    btn.disabled = false;
  }
}
$('wrRefreshBtn').addEventListener('click', () => loadWeeklyReport(true));

// ---------------------------------------------------------------------------
// 주간 리포트 구성 설정 (그룹·종목·MA 기간) — 코드 수정 없이 화면에서 편집
// ---------------------------------------------------------------------------
let wrDraft = null;
let wrDefault = null;

function wrOptions(labels, selected) {
  return Object.entries(labels).map(([v, l]) =>
    `<option value="${v}"${v === selected ? ' selected' : ''}>${esc(l)}</option>`).join('');
}

function renderWeeklyReportEditor() {
  $('wrPeriods').value = wrDraft.ma_periods.join(', ');
  $('wrBenchmark').value = wrDraft.benchmark;
  $('wrRateSymbol').value = wrDraft.rate_symbol;
  $('wrFxSymbol').value = wrDraft.fx_symbol;
  $('wrNearPct').value = wrDraft.comment_rules.near_ma_pct;
  $('wrCalmPct').value = wrDraft.comment_rules.calm_position_pct;
  $('wrTopN').value = wrDraft.comment_rules.sector_top_n;
  const box = $('wrGroupEditor');
  box.innerHTML = '';
  wrDraft.groups.forEach((g, gi) => {
    const el = document.createElement('div');
    el.className = 'wr-group';
    el.innerHTML = `
      <div class="wr-group-head">
        <input data-f="title" value="${esc(g.title)}" placeholder="그룹 이름">
        <select data-f="type">${wrOptions(WR_GROUP_TYPE_LABELS, g.type)}</select>
        <button type="button" class="rb-remove" data-act="up" title="위로">↑</button>
        <button type="button" class="rb-remove" data-act="down" title="아래로">↓</button>
        <button type="button" class="rb-remove" data-act="remove-group">그룹 삭제</button>
      </div>
      <div class="wr-items"></div>
      ${g.type === 'my_account' ? '<div class="rb-hint" style="margin-top:6px;">종목은 토스 보유종목으로 자동으로 채워집니다.</div>'
        : '<button type="button" class="rb-add" data-act="add-item">+ 종목</button>'}`;
    const items = el.querySelector('.wr-items');
    g.items.forEach((it, ii) => {
      const row = document.createElement('div');
      row.className = 'wr-item-row';
      row.innerHTML = `
        <input class="sym" data-f="symbol" value="${esc(it.symbol)}" placeholder="심볼">
        <input class="lbl" data-f="label" value="${esc(it.label)}" placeholder="표시 이름">
        <select data-f="kind">${wrOptions(WR_ITEM_KIND_LABELS, it.kind)}</select>
        <button type="button" class="rb-remove">삭제</button>`;
      row.querySelectorAll('[data-f]').forEach(inp => inp.addEventListener('input', () => { it[inp.dataset.f] = inp.value; }));
      row.querySelector('button').addEventListener('click', () => { g.items.splice(ii, 1); renderWeeklyReportEditor(); });
      items.appendChild(row);
    });
    el.querySelectorAll('.wr-group-head [data-f]').forEach(inp => inp.addEventListener('input', () => {
      g[inp.dataset.f] = inp.value;
      if (inp.dataset.f === 'type') { if (g.type === 'my_account') g.items = []; renderWeeklyReportEditor(); }
    }));
    el.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', () => {
      const act = b.dataset.act;
      if (act === 'add-item') g.items.push({ symbol: '', label: '', kind: 'price' });
      if (act === 'remove-group') wrDraft.groups.splice(gi, 1);
      if (act === 'up' && gi > 0) [wrDraft.groups[gi - 1], wrDraft.groups[gi]] = [wrDraft.groups[gi], wrDraft.groups[gi - 1]];
      if (act === 'down' && gi < wrDraft.groups.length - 1) [wrDraft.groups[gi + 1], wrDraft.groups[gi]] = [wrDraft.groups[gi], wrDraft.groups[gi + 1]];
      renderWeeklyReportEditor();
    }));
    box.appendChild(el);
  });
}

async function loadWeeklyReportConfig() {
  const res = await fetch('/api/weekly-report-config');
  if (!res.ok) return;
  const data = await res.json();
  wrDraft = structuredClone(data.config);
  wrDefault = data.default;
  renderWeeklyReportEditor();
}

$('wrAddGroupBtn').addEventListener('click', () => {
  wrDraft.groups.push({ title: '새 그룹', type: 'basic', items: [] });
  renderWeeklyReportEditor();
});
$('wrResetBtn').addEventListener('click', () => {
  wrDraft = structuredClone(wrDefault);
  renderWeeklyReportEditor();
});
$('wrSaveBtn').addEventListener('click', () => {
  const config = {
    ...wrDraft,
    ma_periods: $('wrPeriods').value.split(/[,\s]+/).filter(Boolean).map(Number),
    benchmark: $('wrBenchmark').value.trim(),
    rate_symbol: $('wrRateSymbol').value.trim(),
    fx_symbol: $('wrFxSymbol').value.trim(),
    comment_rules: {
      near_ma_pct: Number($('wrNearPct').value),
      calm_position_pct: Number($('wrCalmPct').value),
      sector_top_n: Number($('wrTopN').value),
    },
    groups: wrDraft.groups.map(g => ({ ...g, items: g.items.filter(it => it.symbol.trim()) })),
  };
  runSave({
    btn: $('wrSaveBtn'), errEl: $('wrConfigError'), savedEl: $('wrSaved'), url: '/api/weekly-report-config',
    body: { config }, busyText: '확인 중...',
    onSuccess: data => {
      wrDraft = structuredClone(data.config);
      renderWeeklyReportEditor();
      loadWeeklyReport(false);
    },
  });
});
