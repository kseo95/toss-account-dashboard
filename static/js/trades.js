// 거래 페이지: 올해 체결 내역·실현 손익·배당 추정 (조회 전용).

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
