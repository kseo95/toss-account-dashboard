// 관심종목과 MA 감시.

// ---------- 관심종목 ----------
// 추가/삭제는 항상 전체 목록을 다시 저장한다(서버가 토스에 실제로 있는 종목인지 확인).
let watchlistSymbols = [];

function renderWatchlistTable(items) {
  const tbody = $('watchlistRows');
  tbody.innerHTML = items.length ? '' : '<tr class="empty-row"><td colspan="4">관심종목이 없습니다. 위에서 검색해서 추가하세요.</td></tr>';
  items.forEach(it => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${nameWithSymbol(it.name, it.symbol)}</td>
      <td>${fmtPrice(it.price, it.currency)}</td>
      <td class="${signClass(it.change_pct)}">${fmtPct(it.change_pct)}</td>
      <td><button type="button" class="watchlist-remove">삭제</button></td>`;
    tr.querySelector('.watchlist-remove').addEventListener('click', () => saveWatchlistSymbols(watchlistSymbols.filter(s => s !== it.symbol)));
    tbody.appendChild(tr);
  });
}

async function loadWatchlist() {
  const { ok, data } = await getJson('/api/watchlist');
  if (!ok) return;
  watchlistSymbols = (data.items || []).map(it => it.symbol);
  renderWatchlistTable(data.items || []);
}

async function saveWatchlistSymbols(symbols) {
  hideEl($('watchlistError'));
  const { ok, data } = await postJson('/api/watchlist', { symbols });
  if (!ok) return showMsg($('watchlistError'), data.error || '저장에 실패했습니다.');
  await loadWatchlist();
}

attachSymbolSearch({
  input: $('watchlistSearchInput'),
  suggestions: $('watchlistSuggestions'),
  tagFor: r => (watchlistSymbols.includes(r.symbol) ? ' · 이미 등록됨' : ''),
  onPick: r => { if (!watchlistSymbols.includes(r.symbol)) saveWatchlistSymbols([...watchlistSymbols, r.symbol]); },
});

// ---------- MA 감시 ----------
// 지표를 MA 하나로 하드코딩하지 않기 위해 (일봉/주봉, 기간, 근접 기준%)는 전부 사용자가 이 화면에서 정한다.
let maSelected = null; // { symbol, name } - 검색해서 고른 종목(아직 규칙에 추가 전)
let maDraft = [];      // 화면에서 편집 중인 규칙 [{symbol, name, interval, period, proximity_pct}]

const intervalLabel = interval => (interval === 'day' ? '일봉' : '주봉');

function renderMaSelected() {
  $('maSelectedSymbol').textContent = maSelected ? `선택된 종목: ${maSelected.name} (${maSelected.symbol})` : '검색해서 종목을 먼저 선택하세요.';
}

function renderMaDraftList() {
  const list = $('maRuleDraftList');
  list.innerHTML = maDraft.length ? '' : '<div class="muted" style="font-size:13px;">등록된 규칙이 없습니다. 위에서 종목을 검색해 추가하세요.</div>';
  maDraft.forEach((r, i) => {
    const row = document.createElement('div');
    row.className = 'ma-rule-row';
    row.innerHTML = `
      <span>${esc(r.name)} <span class="meta">(${esc(r.symbol)})</span></span>
      <span class="meta">${intervalLabel(r.interval)} ${r.period} · 근접 ${r.proximity_pct}% 이내</span>
      <button type="button" class="rb-remove">삭제</button>`;
    row.querySelector('.rb-remove').addEventListener('click', () => {
      maDraft.splice(i, 1);
      renderMaDraftList();
    });
    list.appendChild(row);
  });
}

function renderMaResults(rows) {
  const tbody = $('maResultRows');
  tbody.innerHTML = rows.length ? '' : '<tr class="empty-row"><td colspan="5">등록된 MA 규칙이 없습니다.</td></tr>';
  rows.forEach(r => {
    const tr = document.createElement('tr');
    const head = `<td>${nameWithSymbol(r.name, r.symbol)}</td><td>${intervalLabel(r.interval)} ${r.period}</td>`;
    tr.innerHTML = r.ok
      ? `${head}<td>${fmtPrice(r.ma_value, r.currency)}</td><td class="${signClass(r.diff_pct)}">${fmtPct(r.diff_pct)}</td><td>${r.hit ? '✅' : '—'}</td>`
      : `${head}<td colspan="3" class="muted" style="text-align:left;">${esc(r.reason)}</td>`;
    tbody.appendChild(tr);
  });
}

async function loadMaRules() {
  const { ok, data } = await getJson('/api/ma-rules');
  if (!ok) return;
  const rows = data.rows || [];
  maDraft = rows.map(({ symbol, name, interval, period, proximity_pct }) => ({ symbol, name, interval, period, proximity_pct }));
  renderMaDraftList();
  renderMaResults(rows);
}

attachSymbolSearch({
  input: $('maSearchInput'),
  suggestions: $('maSuggestions'),
  onPick: r => { maSelected = { symbol: r.symbol, name: r.name }; renderMaSelected(); },
});

$('addMaRuleBtn').addEventListener('click', () => {
  hideEl($('maError'));
  if (!maSelected) return showMsg($('maError'), '먼저 종목을 검색해서 선택하세요.');
  maDraft.push({
    ...maSelected,
    interval: $('maIntervalSelect').value,
    period: parseInt($('maPeriodInput').value, 10),
    proximity_pct: parseFloat($('maProximityInput').value),
  });
  maSelected = null;
  renderMaSelected();
  renderMaDraftList();
});

$('saveMaRulesBtn').addEventListener('click', () => runSave({
  btn: $('saveMaRulesBtn'), errEl: $('maError'), savedEl: $('maRulesSaved'), url: '/api/ma-rules',
  body: { rules: maDraft.map(({ symbol, interval, period, proximity_pct }) => ({ symbol, interval, period, proximity_pct })) },
  onSuccess: loadMaRules,
}));

renderMaSelected();
