// 포트폴리오: 요약 카드, 보유 비중·원/달러 도넛, 리밸런싱 목표, 현금, 국내/해외 종목 표.

let cashSymbolsDraft = new Set();  // 현금 취급 종목 설정 (체크박스 화면용 임시 상태)
let heldSymbolsInfo = [];          // 현재 보유 중인 전체 종목 [{symbol, name, qty}]
let usCurrencyMode = 'krw';        // 해외 종목 표 'krw' | 'usd' (재조회 없이 캐시된 행으로 다시 그림)
let lastUsStocks = [];

// ---------- 헤더 환율 ----------
// "USD/KRW 1,391.20   USD/JPY 147.33 (23:17 기준)" (출처는 화면에 안 보여줌, 시각만 표시)
function renderFxDisplay(usdKrw, usdJpy) {
  let text = `USD/KRW ${usdKrw.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
  if (usdJpy) text += `   USD/JPY ${usdJpy.rate.toLocaleString('en-US', { maximumFractionDigits: 2 })} (${usdJpy.as_of} 기준)`;
  $('fxDisplay').textContent = text;
}

// ---------- 국내/해외 종목 표 ----------
function stockRow(r, evalText, plText, plValue) {
  return `
    <td>${nameWithSymbol(r['종목'], r['심볼'])}</td>
    <td>${r['수량'].toLocaleString('ko-KR')}</td>
    <td>${evalText}</td>
    <td class="${signClass(plValue)}">${plText}</td>
    <td class="${signClass(r['수익률(%)'])}">${fmtPct(r['수익률(%)'])}</td>
    <td>${r['지분율(%)'].toFixed(2)}%</td>`;
}

function renderStockTable(tbody, rows, cellsFor) {
  tbody.innerHTML = rows.length ? '' : '<tr class="empty-row"><td colspan="6">보유 종목이 없습니다.</td></tr>';
  rows.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = cellsFor(r);
    tbody.appendChild(tr);
  });
}

function renderKrRows(rows) {
  renderStockTable($('krRows'), rows, r => stockRow(r, fmtWon(r['평가금액(원)']), fmtWon(r['손익(원)']), r['손익(원)']));
}

function renderUsRows(rows) {
  lastUsStocks = rows;
  const usd = usCurrencyMode === 'usd';
  $('usEvalHeader').textContent = usd ? '평가금액($)' : '평가금액(원)';
  $('usPlHeader').textContent = usd ? '손익($)' : '손익(원)';
  const fmt = v => (v == null ? '-' : (usd ? fmtUsd(v) : fmtWon(v)));
  renderStockTable($('usRows'), rows, r => {
    const pl = usd ? r['손익(외화)'] : r['손익(원)'];
    return stockRow(r, fmt(usd ? r['평가금액(외화)'] : r['평가금액(원)']), fmt(pl), pl || 0);
  });
}

document.querySelectorAll('#usCurrencyToggle button').forEach(btn => {
  btn.addEventListener('click', () => {
    usCurrencyMode = btn.dataset.mode;
    document.querySelectorAll('#usCurrencyToggle button').forEach(b => b.classList.toggle('active', b === btn));
    renderUsRows(lastUsStocks);
  });
});

// ---------- 보유 비중 도넛 / 원-달러 비율 도넛 ----------
// dataviz 스킬의 검증된 다크모드 카테고리 팔레트 (real/dashboard.html과 같은 값).
const CAT_PALETTE = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767',
  '#76589d', '#8f5d04', '#0c71a0'];
const CASH_COLOR = '#898781'; // "현금"은 실제 종목이 아니라서 중립색
const charts = {};
let lastRebalance = [];
let lastCurrencySplit = { krw_pct: 0, usd_pct: 0, krw_target_pct: 0, usd_target_pct: 0 };
const fmtShortPct = v => (Math.round(v * 10) / 10) + '%';

// Chart.js 내장 legend(클릭하면 조각 토글) + generateLabels로 "현재% / 목표%"를 붙인다.
function donutOptions(generateLabels) {
  return {
    maintainAspectRatio: false,
    plugins: {
      legend: {
        position: 'right',
        labels: { color: '#e6edf3', boxWidth: 12, generateLabels },
        onClick: (e, item, legend) => {
          if (item.extra) return; // 목표만 있고 도넛 조각이 없는(0%) 항목은 토글할 게 없음
          Chart.overrides.doughnut.plugins.legend.onClick.call(legend, e, item, legend);
        },
      },
      datalabels: { color: '#0d1117', font: { weight: 'bold', size: 12 }, formatter: v => (v >= 1 ? v.toFixed(1) + '%' : '') },
    },
  };
}

function drawDonut(key, canvasId, items, legendLabels) {
  const labels = items.map(it => it.label);
  const values = items.map(it => it.pct);
  const colors = items.map(it => it.color);
  const chart = charts[key];
  if (!chart) {
    charts[key] = new Chart($(canvasId), {
      type: 'doughnut',
      data: { labels, datasets: [{ data: values, backgroundColor: colors }] },
      plugins: [ChartDataLabels],
      options: donutOptions(legendLabels),
    });
    return;
  }
  chart.data.labels = labels;
  chart.data.datasets[0].data = values;
  chart.data.datasets[0].backgroundColor = colors; // 정렬 순서가 바뀌어도 색이 항목을 따라가게
  chart.update();
}

function categoryColorFor(label) {
  if (label === '나머지' || label === '현금') return CASH_COLOR;
  const idx = lastRebalance.filter(r => r.label !== '나머지' && r.label !== '현금').findIndex(r => r.label === label);
  return CAT_PALETTE[idx % CAT_PALETTE.length];
}

// 범례: "분류  현재% / 목표%". 현재 0%(도넛에 조각 없음)인 카테고리는 회색으로 범례에만 추가.
function allocLegendLabels(chart) {
  const byLabel = Object.fromEntries(lastRebalance.map(r => [r.label, r]));
  const base = Chart.overrides.doughnut.plugins.legend.labels.generateLabels(chart);
  const shown = new Set(base.map(it => it.text));
  base.forEach(it => {
    const r = byLabel[it.text];
    if (r) it.text = `${it.text}  ${fmtShortPct(r.current_pct)} / ${fmtShortPct(r.target_pct)}`;
  });
  const extras = lastRebalance
    .filter(r => !shown.has(r.label) && r.target_pct > 0)
    .sort((a, b) => b.target_pct - a.target_pct)
    .map(r => ({ text: `${r.label}  0% / ${fmtShortPct(r.target_pct)}`, fillStyle: categoryColorFor(r.label),
      strokeStyle: categoryColorFor(r.label), fontColor: '#8b949e', hidden: false, extra: true }));
  return base.concat(extras);
}

function renderAllocChart(rebalance) {
  lastRebalance = rebalance;
  const items = rebalance
    .filter(r => r.current_pct > 0.01) // 아직 안 산(0%) 카테고리는 범례에만
    .sort((a, b) => b.current_pct - a.current_pct)
    .map(r => ({ label: r.label, pct: r.current_pct, color: categoryColorFor(r.label) }));
  drawDonut('alloc', 'allocChart', items, allocLegendLabels);
}

function currencyLegendLabels(chart) {
  const s = lastCurrencySplit;
  const byLabel = { '원화': [s.krw_pct, s.krw_target_pct], '달러': [s.usd_pct, s.usd_target_pct] };
  const base = Chart.overrides.doughnut.plugins.legend.labels.generateLabels(chart);
  base.forEach(it => {
    const r = byLabel[it.text];
    if (r) it.text = `${it.text}  ${fmtShortPct(r[0])} / ${fmtShortPct(r[1])}`;
  });
  return base;
}

function renderCurrencyChart(split) {
  lastCurrencySplit = split;
  const items = [
    { label: '원화', pct: split.krw_pct, color: '#3987e5' },
    { label: '달러', pct: split.usd_pct, color: '#199e70' },
  ].sort((a, b) => b.pct - a.pct);
  drawDonut('currency', 'currencyChart', items, currencyLegendLabels);
}

// 두 도넛을 동시에 보여주면 legend 길이 차이로 크기가 달라 보여서 탭으로 하나씩만 보여준다. 숨겨져 있던
// 캔버스가 다시 보일 때 Chart.js가 크기를 다시 계산하도록 resize()를 호출한다.
document.querySelectorAll('.chart-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.chart-tab').forEach(t => t.classList.toggle('active', t === tab));
    const showAlloc = tab.dataset.tab === 'alloc';
    $('allocTabPanel').style.display = showAlloc ? '' : 'none';
    $('currencyTabPanel').style.display = showAlloc ? 'none' : '';
    charts[showAlloc ? 'alloc' : 'currency']?.resize();
  });
});

// ---------- 리밸런싱 목표 설정 ----------
// 카테고리로 안 묶인 종목/예수금은 각각 개별 항목으로 표시되고 목표%는 전부 "기본 목표%"로 고정된다.
let rebalanceDraft = { targets: [], default_rest_target_pct: 5, krw_target_pct: 50 };

function setRebalanceDraft(data) {
  rebalanceDraft = {
    targets: (data.targets || []).map(t => ({ ...t, symbols: [...t.symbols] })),
    default_rest_target_pct: data.default_rest_target_pct,
    krw_target_pct: data.krw_target_pct,
  };
  renderRebalancePanel();
}

function updateRebalanceSum() {
  const categorySum = rebalanceDraft.targets.reduce((s, t) => s + (parseFloat(t.target_pct) || 0), 0);
  $('rebalanceSum').textContent = `카테고리 목표% 합계: ${Math.round(categorySum * 10) / 10}% (미분류 종목/예수금은 위 "기본 목표%"로 각각 표시됩니다)`;
  const krwTarget = parseFloat($('krwTargetInput').value) || 0;
  $('usdTargetHint').textContent = `(달러 목표: ${Math.round((100 - krwTarget) * 10) / 10}%)`;
}

function renderRebalancePanel() {
  const container = $('rebalanceCategories');
  container.innerHTML = '';
  rebalanceDraft.targets.forEach((t, i) => {
    const row = document.createElement('div');
    row.className = 'rb-category-row';
    row.innerHTML = `
      <input type="text" placeholder="카테고리 이름" value="${esc(t.label)}" data-field="label">
      <input type="text" placeholder="종목 코드 (쉼표로 구분, 예: 069500, 114800)" value="${esc(t.symbols.join(', '))}" data-field="symbols">
      <input type="number" min="0" max="100" step="0.1" value="${t.target_pct}" data-field="target_pct">
      <button type="button" class="rb-remove">삭제</button>`;
    const field = name => row.querySelector(`[data-field="${name}"]`);
    field('label').addEventListener('input', e => { t.label = e.target.value; });
    field('symbols').addEventListener('input', e => { t.symbols = parseSymbolList(e.target.value); });
    field('target_pct').addEventListener('input', e => { t.target_pct = parseFloat(e.target.value) || 0; updateRebalanceSum(); });
    row.querySelector('.rb-remove').addEventListener('click', () => {
      rebalanceDraft.targets.splice(i, 1);
      renderRebalancePanel();
    });
    container.appendChild(row);
  });
  $('restPctInput').value = rebalanceDraft.default_rest_target_pct;
  $('krwTargetInput').value = rebalanceDraft.krw_target_pct;
  updateRebalanceSum();
}

async function loadRebalanceConfig() {
  const { ok, data } = await getJson('/api/rebalance-config');
  if (ok) setRebalanceDraft(data);
}

// 카테고리 설정과 원/달러 목표는 같은 rebalance.json을 쓰는 다른 화면이라, 어느 버튼을 눌러도 전체를 저장한다.
function saveRebalanceSettings(errId, savedId, btnId) {
  return runSave({
    btn: $(btnId), errEl: $(errId), savedEl: $(savedId), url: '/api/rebalance-config',
    body: {
      targets: rebalanceDraft.targets,
      default_rest_target_pct: parseFloat($('restPctInput').value) || 0,
      krw_target_pct: parseFloat($('krwTargetInput').value) || 0,
    },
    onSuccess: async data => {
      setRebalanceDraft(data);
      await loadHoldings(); // 저장된 목표를 도넛/범례에 바로 반영
    },
  });
}

$('restPctInput').addEventListener('input', updateRebalanceSum);
$('krwTargetInput').addEventListener('input', updateRebalanceSum);
$('addCategoryBtn').addEventListener('click', () => {
  rebalanceDraft.targets.push({ label: '', symbols: [], target_pct: 0 });
  renderRebalancePanel();
});
$('resetRebalanceBtn').addEventListener('click', loadRebalanceConfig);
$('saveRebalanceBtn').addEventListener('click', () => saveRebalanceSettings('rebalanceError', 'rebalanceSaved', 'saveRebalanceBtn'));
$('saveCurrencyTargetBtn').addEventListener('click', () => saveRebalanceSettings('currencyTargetError', 'currencyTargetSaved', 'saveCurrencyTargetBtn'));

// ---------- 현금 ----------
function renderCashTable(cash) {
  const rows = [];
  if (cash.krw) rows.push(['예수금 (원화)', cash.krw]);
  if (cash.usd) rows.push([`예수금 (달러, ${fmtUsd(cash.usd)} · 환율 ${cash.usd_krw_rate.toFixed(2)})`, cash.usd * cash.usd_krw_rate]);
  (cash.stocks || []).forEach(r => rows.push([`${esc(r['종목'])} (${esc(r['심볼'])})`, r['평가금액(원)']]));
  $('cashRows').innerHTML =
    (rows.length ? rows.map(([label, amount]) => `<tr><td>${label}</td><td>${fmtWon(amount)}</td></tr>`).join('')
      : '<tr class="empty-row"><td colspan="2">현금이 없습니다.</td></tr>') +
    `<tr><td><strong>합계</strong></td><td><strong>${fmtWon(cash.total_krw)}</strong></td></tr>`;
}

function renderCashSymbolPicker() {
  const picker = $('cashSymbolPicker');
  picker.innerHTML = '';
  const held = new Set(heldSymbolsInfo.map(s => s.symbol));

  // 1) 현재 보유 중인 종목: 체크박스로 선택
  heldSymbolsInfo.forEach(s => {
    const row = document.createElement('label');
    row.className = 'cash-symbol-row';
    row.innerHTML = `
      <input type="checkbox" ${cashSymbolsDraft.has(s.symbol) ? 'checked' : ''}>
      <span>${esc(s.name)}</span>
      <span class="name">(${esc(s.symbol)}, ${s.qty.toLocaleString('ko-KR')}주)</span>`;
    row.querySelector('input').addEventListener('change', e => {
      if (e.target.checked) cashSymbolsDraft.add(s.symbol);
      else cashSymbolsDraft.delete(s.symbol);
    });
    picker.appendChild(row);
  });

  // 2) 설정은 돼 있지만 지금은 미보유인 종목: 해제 버튼만 제공
  [...cashSymbolsDraft].filter(sym => !held.has(sym)).forEach(sym => {
    const row = document.createElement('div');
    row.className = 'cash-symbol-row';
    row.innerHTML = `<span>${esc(sym)}</span><span class="unheld-tag">미보유 · 0주 · 설정만 남아있음</span>
      <button class="unset" type="button">해제</button>`;
    row.querySelector('button').addEventListener('click', () => {
      cashSymbolsDraft.delete(sym);
      renderCashSymbolPicker();
    });
    picker.appendChild(row);
  });

  if (!heldSymbolsInfo.length && !cashSymbolsDraft.size) picker.innerHTML = '<div class="muted">보유 종목이 없습니다.</div>';
}

$('saveCashSymbolsBtn').addEventListener('click', () => runSave({
  btn: $('saveCashSymbolsBtn'), errEl: $('cashSymbolsError'), savedEl: $('cashSymbolsSaved'),
  url: '/api/cash-symbols', body: { symbols: [...cashSymbolsDraft] },
  onSuccess: loadHoldings, // 현금/종목 표 다시 계산
}));

// ---------- 불러오기 ----------
async function loadHoldings() {
  const banner = $('dashboardError');
  let holdings, cashSymbols;
  try {
    [holdings, cashSymbols] = await Promise.all([getJson('/api/holdings'), getJson('/api/cash-symbols')]);
  } catch (e) {
    return showMsg(banner, '서버에 연결할 수 없습니다.');
  }
  if (holdings.status === 401) return showLoggedOut(); // 세션이 실제로 만료된 경우에만 로그인 화면으로
  if (!holdings.ok) return showMsg(banner, holdings.data.error || `보유종목 조회에 실패했습니다 (${holdings.status}).`);
  hideEl(banner);

  const data = holdings.data;
  renderKrRows(data.stocks.filter(r => r['시장'] === 'KR'));
  renderUsRows(data.stocks.filter(r => r['시장'] !== 'KR'));
  renderCashTable(data.cash);
  renderAllocChart(data.rebalance);
  renderCurrencyChart(data.currency_split);
  renderFxDisplay(data.usd_krw, data.usd_jpy);

  const t = data.totals;
  $('totalEval').textContent = fmtWon(t.eval_krw);
  $('totalPl').textContent = fmtWon(t.profit_loss_krw);
  $('totalPl').className = 'value ' + signClass(t.profit_loss_krw);
  $('totalRate').textContent = fmtPct(t.rate_pct);
  $('totalRate').className = 'value ' + signClass(t.rate_pct);

  heldSymbolsInfo = [...data.stocks, ...data.cash.stocks].map(r => ({ symbol: r['심볼'], name: r['종목'], qty: r['수량'] }));
  cashSymbolsDraft = new Set(cashSymbols.ok ? cashSymbols.data.symbols || [] : []);
  renderCashSymbolPicker();
}
