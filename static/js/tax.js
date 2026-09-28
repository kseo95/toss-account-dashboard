// 세금 페이지: 지금 다 팔면 세금(추정), 종목별 근거, 올해 실현 손익·세율 설정, 세금 한눈에 표.
// 계산은 서버(tax.py)가 하고 여기서는 그리기 + 설정 편집만.

let taxData = null;   // 마지막 /api/tax 응답
let taxDraft = null;  // 편집 중인 설정

const TAX_FIELDS = [ // [입력 id, 설정 키, 하위 키]
  ['taxRealized', 'realized_overseas_gain_ytd_krw'], ['taxFinIncome', 'financial_income_ytd_krw'],
  ['taxKospi', 'transaction_tax_pct', 'KOSPI'], ['taxKosdaq', 'transaction_tax_pct', 'KOSDAQ'],
  ['taxKonex', 'transaction_tax_pct', 'KONEX'], ['taxOverseas', 'overseas_gain_rate_pct'],
  ['taxDeduction', 'gain_deduction_krw'], ['taxEtfOther', 'etf_other_rate_pct'],
  ['taxDividend', 'dividend_rate_pct'], ['taxThreshold', 'financial_income_threshold_krw'],
];

// 기본공제(보통 250만 원)를 100%로 놓고, 올해 이미 판 수익(확정)과 아직 안 판 수익(평가)이 얼마나 찼는지.
function renderTaxGauge(s, settings) {
  const deduction = settings.gain_deduction_krw;
  const rate = settings.overseas_gain_rate_pct;
  const realized = s.realized_overseas_gain_ytd_krw;
  const total = realized + s.unrealized_pooled_gain_krw;
  const pct = v => (deduction > 0 ? (v / deduction) * 100 : 0);
  // 막대 눈금: 공제선과 합계 중 큰 쪽을 꽉 찬 폭으로(공제선 위치가 보이게 최소 110%).
  const scale = Math.max(deduction * 1.1, total, realized);
  const w = v => `${Math.max(0, Math.min(100, (v / scale) * 100))}%`;
  const realizedPos = Math.max(0, realized);
  const unrealizedPos = Math.max(0, total - realizedPos);
  const left = deduction - realized;
  const overAll = Math.max(0, total - deduction);
  const netAfterTax = total - overAll * rate / 100;

  const headline = realized >= deduction
    ? `올해 판 수익이 이미 공제를 <b class="loss">${fmtWon(realized - deduction)}</b> 넘었어요 — 넘은 만큼 ${rate}% 과세`
    : `올해 판 수익 <b>${fmtWon(realized)}</b> → 공제의 <b>${pct(realized).toFixed(0)}%</b> 사용 · ` +
      `<b class="profit">${fmtWon(left)}</b> 더 벌고 팔아도 세금 0원`;
  const ifAll = total <= deduction
    ? `지금 다 팔아도 올해 수익 ${fmtWon(total)} (공제의 ${pct(total).toFixed(0)}%) — <b class="profit">세금 0원</b>`
    : `지금 다 팔면 올해 수익 ${fmtWon(total)} (공제의 <b>${pct(total).toFixed(0)}%</b>) → 넘는 ${fmtWon(overAll)} × ${rate}% = ` +
      `<b class="loss">${fmtWon(overAll * rate / 100)}</b>`;
  $('taxGauge').innerHTML = `
    <div class="tax-gauge">
      <div class="bar">
        <span class="seg realized" style="width:${w(realizedPos)}"></span><span class="seg unrealized" style="width:${w(unrealizedPos)}"></span>
        <span class="line" style="left:${w(deduction)}"><em>공제 ${fmtWon(deduction)}</em></span>
      </div>
      <div class="legend"><span class="dot realized"></span>올해 이미 판 수익 <span class="dot unrealized"></span>아직 안 판 수익(지금 팔면)</div>
    </div>
    <div class="wr-summary">
      <div>${headline}</div>
      <div>${ifAll}</div>
      <div class="muted">다 팔고 세금 낸 뒤 남는 올해 수익 ≈ ${fmtWon(netAfterTax)}${total > 0 ? ` (세금 ${((1 - netAfterTax / total) * 100).toFixed(1)}%)` : ''}</div>
    </div>`;
}

function renderTaxSummary(s) {
  const line = (label, value) => `<div><span class="label">${label}</span>${value}</div>`;
  const signed = v => `<span class="${signClass(v)}">${fmtWon(v)}</span>`;
  $('taxSummary').innerHTML = [
    line('지금 전부 팔면 세금', `<b>${fmtWon(s.tax_if_sell_all_krw)}</b> <span class="muted">(해외 양도세 ${fmtWon(s.gain_tax_if_sell_all_krw)} + 국내 거래세·ETF ${fmtWon(s.domestic_tax_if_sell_all_krw)})</span>`),
    line('해외 주식 미실현 손익', signed(s.unrealized_pooled_gain_krw)),
    line(`올해 이미 판 손익 (${taxData.realized_source === 'auto' ? '체결 내역 자동' : '직접 입력'})`,
      `${signed(s.realized_overseas_gain_ytd_krw)} → 이미 확정된 세금 ${fmtWon(s.tax_on_realized_krw)}` +
      (taxData.realized_note ? `<div class="loss" style="font-size:12px;">${esc(taxData.realized_note)}</div>` : '')),
    line('남은 기본공제', `${fmtWon(s.deduction_left_krw)} <span class="muted">(이만큼은 올해 더 벌고 팔아도 세금 없음)</span>`),
    line(`올해 이자·배당 (${taxData.income_source === 'auto' ? '배당 추정, 세전' : '직접 입력'})`,
      `${fmtWon(s.financial_income_ytd_krw)} <span class="muted">· 종합과세(2,000만 원)까지 ${fmtWon(s.financial_income_left_krw)} 남음` +
      `${taxData.income_source === 'auto' ? ' · 예수금 이자는 미포함' : ''}</span>` +
      (taxData.income_note ? `<div class="loss" style="font-size:12px;">${esc(taxData.income_note)}</div>` : '')),
  ].join('');
}

function renderTaxRows(rows, classes) {
  const tbody = $('taxRows');
  tbody.innerHTML = rows.length ? '' : '<tr class="empty-row"><td colspan="5">보유 종목이 없습니다.</td></tr>';
  rows.forEach(r => {
    const tr = document.createElement('tr');
    const kind = r.kind === 'overseas'
      ? esc(r.kind_label)
      : `<select>${Object.entries(classes).map(([k, l]) => `<option value="${k}"${k === r.kind ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select>` +
        (r.class_guessed ? '<span class="strategy-sub">이름으로 추정</span>' : '');
    tr.innerHTML = `
      <td class="name">${nameWithSymbol(r.name, r.symbol)}${r.notes.map(n => `<span class="strategy-sub">${esc(n)}</span>`).join('')}</td>
      <td>${kind}</td>
      <td>${fmtWon(r.value_krw)}</td>
      <td class="${signClass(r.gain_krw)}">${fmtWon(r.gain_krw)}</td>
      <td><b>${fmtWon(r.tax_krw)}</b></td>`;
    tr.querySelector('select')?.addEventListener('change', e => { taxDraft.domestic_classes[r.symbol] = e.target.value; });
    tbody.appendChild(tr);
  });
}

function renderTaxReference(ref) {
  $('taxReference').innerHTML = ref.map(x =>
    `<tr><td>${esc(x.what)}</td><td>${esc(x.rate)}</td><td>${esc(x.when)}</td><td class="muted">${esc(x.law)}</td></tr>`).join('');
}

function fillTaxInputs() {
  TAX_FIELDS.forEach(([id, key, sub]) => { $(id).value = sub ? taxDraft[key][sub] : taxDraft[key]; });
  $('taxMajor').checked = taxDraft.is_major_shareholder;
  $('taxManualRealized').checked = taxDraft.use_manual_realized;
  $('taxManualIncome').checked = taxDraft.use_manual_financial_income;
}

async function loadTax() {
  const status = $('taxStatus');
  status.className = 'wr-status';
  status.textContent = '계산 중...';
  status.style.display = 'block';
  $('taxRefreshBtn').disabled = true;
  try {
    const { ok, data } = await getJson('/api/tax');
    if (!ok) {
      status.className = 'wr-status error';
      status.textContent = data.error || '세금을 계산하지 못했습니다.';
      return;
    }
    hideEl(status);
    taxData = data;
    taxDraft = structuredClone(data.settings);
    renderTaxGauge(data.summary, data.settings);
    renderTaxSummary(data.summary);
    renderTaxRows(data.rows, data.classes);
    renderTaxReference(data.reference);
    fillTaxInputs();
  } catch (e) {
    status.className = 'wr-status error';
    status.textContent = '서버에 연결할 수 없습니다.';
  } finally {
    $('taxRefreshBtn').disabled = false;
  }
}

// ---------- 올해 체결 내역 ----------
function fmtNative(v, currency) {
  return currency === 'USD' ? fmtUsd(v) : fmtWon(v);
}

function renderTrades(data) {
  const s = data.summary;
  $('tradesPeriod').textContent = data.fills.length
    ? `${data.year}년 · ${(s.first_filled_at || '').slice(0, 10)} ~ ${(s.last_filled_at || '').slice(0, 10)}`
    : `${data.year}년 체결 내역이 없습니다.`;
  // 국내→해외, 매수→매도 순서로 한 줄씩
  const order = k => (k.currency === 'USD' ? 2 : 0) + (k.side === 'SELL' ? 1 : 0);
  $('tradesSummaryRows').innerHTML = [...s.by_kind].sort((a, b) => order(a) - order(b)).map(k => `<tr>
      <td>${k.currency === 'USD' ? '해외' : '국내'} ${k.side === 'BUY' ? '<span class="profit">매수</span>' : '<span class="loss">매도</span>'}</td>
      <td>${k.count}건</td><td>${fmtNative(k.amount, k.currency)}</td><td>${fmtNative(k.commission, k.currency)}</td>
      <td>${k.tax ? fmtNative(k.tax, k.currency) : '–'}</td></tr>`).join('');
  renderRealized(data.realized);
  $('tradesRows').innerHTML = data.fills.map(f => `<tr>
      <td>${esc(f.filled_at.slice(0, 16).replace('T', ' '))}</td>
      <td>${nameWithSymbol(f.name, f.symbol)}</td>
      <td class="${f.side === 'BUY' ? 'profit' : 'loss'}">${f.side === 'BUY' ? '매수' : '매도'}</td>
      <td>${f.quantity.toLocaleString('ko-KR', { maximumFractionDigits: 6 })}</td>
      <td>${fmtPrice(f.price, f.currency)}</td>
      <td>${fmtNative(f.amount, f.currency)}</td>
      <td>${fmtNative(f.commission, f.currency)}</td>
      <td>${f.tax ? fmtNative(f.tax, f.currency) : '–'}</td>
      <td>${esc(f.settlement_date || '–')}</td></tr>`).join('');
}

function renderRealized(r) {
  if (!r) {
    $('realizedSummary').innerHTML = '<div class="loss">환율을 불러오지 못해 실현 손익을 계산하지 못했어요.</div>';
    $('realizedRows').innerHTML = '';
    return;
  }
  const warn = [];
  if (r.missing_buys.length) {
    warn.push(`매수 기록보다 많이 판 매도가 있어요(${r.missing_buys.map(m => `${esc(m.symbol)} ${m.quantity}주`).join(', ')}) — ` +
      '예전 거래가 API에 없거나 입고된 주식일 수 있어요. 이 매도는 합계에서 빠졌어요.');
  }
  if (r.missing_fx.length) warn.push(`환율 기록이 없는 날짜(${r.missing_fx.slice(0, 3).map(esc).join(', ')}${r.missing_fx.length > 3 ? ' 등' : ''})의 거래는 합계에서 빠졌어요.`);
  $('realizedSummary').innerHTML =
    `<div><span class="label">${r.year}년 실현 손익</span><b class="${signClass(r.total_gain_krw)}">${fmtWon(r.total_gain_krw)}</b>` +
    ` <span class="muted">(매도 ${r.sells.length}건${r.unknown_count ? `, 계산 못 한 ${r.unknown_count}건 제외` : ''})</span></div>` +
    warn.map(w => `<div class="loss" style="font-size:12px;">${w}</div>`).join('');
  $('realizedRows').innerHTML = [...r.sells].reverse().map(s => `<tr>
      <td>${esc(s.settlement_date)}</td><td>${nameWithSymbol(s.name || s.symbol, s.symbol)}</td>
      <td>${s.quantity.toLocaleString('ko-KR', { maximumFractionDigits: 6 })}</td>
      <td>${s.proceeds_krw == null ? '–' : fmtWon(s.proceeds_krw)}</td>
      <td>${s.cost_krw == null ? '<span class="loss">기록 없음</span>' : fmtWon(s.cost_krw)}</td>
      <td class="${signClass(s.gain_krw || 0)}">${s.gain_krw == null ? '–' : fmtWon(s.gain_krw)}</td>
      <td>${s.fx == null ? '–' : s.fx.toLocaleString('en-US', { maximumFractionDigits: 2 })}</td></tr>`).join('')
    || '<tr class="empty-row"><td colspan="7">올해 해외 매도가 없습니다.</td></tr>';
}

async function loadTrades() {
  const status = $('tradesStatus');
  status.className = 'wr-status';
  status.textContent = '체결 내역 불러오는 중...';
  status.style.display = 'block';
  try {
    const { ok, data } = await getJson('/api/trades');
    if (!ok) {
      status.className = 'wr-status error';
      status.textContent = data.error || '체결 내역을 불러오지 못했습니다.';
      return;
    }
    hideEl(status);
    renderTrades(data);
  } catch (e) {
    status.className = 'wr-status error';
    status.textContent = '서버에 연결할 수 없습니다.';
  }
}

// ---------- 올해 배당 (추정) ----------
async function loadDividends() {
  const status = $('dividendsStatus');
  status.className = 'wr-status';
  status.textContent = '배당 추정 중... (종목별 Yahoo 기록을 받아 몇 초 걸려요)';
  status.style.display = 'block';
  try {
    const { ok, data } = await getJson('/api/dividends');
    if (!ok) {
      status.className = 'wr-status error';
      status.textContent = data.error || '배당을 추정하지 못했습니다.';
      return;
    }
    hideEl(status);
    const line = (label, value) => `<div><span class="label">${label}</span>${value}</div>`;
    $('dividendsSummary').innerHTML =
      line(`${data.year}년 배당 (세전)`, `<b>${fmtWon(data.total_gross_krw)}</b> <span class="muted">${data.rows.length}건</span>`) +
      line('원천징수', `${fmtWon(data.total_withheld_krw)} <span class="muted">(미국 15% · 국내 15.4%, 받을 때 이미 떼임)</span>`) +
      line('세후 입금 (추정)', `<b class="profit">${fmtWon(data.total_net_krw)}</b>`) +
      data.notes.map(n => `<div class="muted" style="font-size:12px;">${esc(n)}</div>`).join('');
    $('dividendsRows').innerHTML = data.rows.map(r => `<tr>
        <td>${esc(r.ex_date)}</td><td>${nameWithSymbol(r.name || r.symbol, r.symbol)}</td>
        <td>${r.shares.toLocaleString('ko-KR', { maximumFractionDigits: 6 })}</td>
        <td>${r.currency === 'USD' ? '$' + r.per_share.toFixed(4) : fmtWon(r.per_share)}</td>
        <td>${fmtNative(r.gross, r.currency)}</td>
        <td>${r.withheld_krw == null ? '–' : fmtWon(r.withheld_krw)} <span class="muted">(${r.withholding_pct}%)</span></td>
        <td>${r.net_krw == null ? '–' : fmtWon(r.net_krw)}</td></tr>`).join('')
      || '<tr class="empty-row"><td colspan="7">올해 배당 기록이 없습니다.</td></tr>';
  } catch (e) {
    status.className = 'wr-status error';
    status.textContent = '서버에 연결할 수 없습니다.';
  }
}

$('taxRefreshBtn').addEventListener('click', () => { loadTax(); loadTrades(); loadDividends(); });
$('taxResetBtn').addEventListener('click', () => {
  // 세율만 기본값으로(올해 입력값·종목 종류는 그대로)
  const d = taxData.default;
  Object.assign(taxDraft, {
    transaction_tax_pct: { ...d.transaction_tax_pct }, overseas_gain_rate_pct: d.overseas_gain_rate_pct,
    gain_deduction_krw: d.gain_deduction_krw, etf_other_rate_pct: d.etf_other_rate_pct,
    dividend_rate_pct: d.dividend_rate_pct, financial_income_threshold_krw: d.financial_income_threshold_krw,
  });
  fillTaxInputs();
});
$('taxSaveBtn').addEventListener('click', () => {
  TAX_FIELDS.forEach(([id, key, sub]) => {
    const v = Number($(id).value);
    if (sub) taxDraft[key][sub] = v; else taxDraft[key] = v;
  });
  taxDraft.is_major_shareholder = $('taxMajor').checked;
  taxDraft.use_manual_realized = $('taxManualRealized').checked;
  taxDraft.use_manual_financial_income = $('taxManualIncome').checked;
  runSave({
    btn: $('taxSaveBtn'), errEl: $('taxError'), savedEl: $('taxSaved'), url: '/api/tax-settings',
    body: { settings: taxDraft }, onSuccess: loadTax,
  });
});
