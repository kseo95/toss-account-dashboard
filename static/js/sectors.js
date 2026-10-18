// 섹터 노출 표와 섹터 설정(섹터 목록 + 종목 배정). portfolio.js 다음, watch.js 앞에 로드된다.
// 섹터 분류와 노출%는 사용자가 직접 정한다. ETF의 노출%는 추정치다.

// ---------- 노출 표 ----------
function renderSectorExposure(data) {
  const status = $('sectorExposureStatus');
  const rows = data.sectors || [];
  // 배정 목록에 종목 이름을 보여주기 위해, 이미 보유 중인 종목의 이름을 기억해 둔다.
  rows.forEach(s => (s.holdings || []).forEach(h => { sectorNames[h.symbol] = h.name; }));
  (data.unmapped || []).forEach(u => { sectorNames[u.symbol] = u.name; });
  status.textContent = data.configured ? '' : '아직 섹터가 없습니다. 아래 "섹터 설정"에서 섹터를 만들고 보유종목을 연결하세요.';
  $('sectorExposureRows').innerHTML = rows.map(s => {
    const holdings = (s.holdings || []).map(h => `${esc(h.name)} ${h.pct}%`).join(', ') || '<span class="muted">연결된 보유종목 없음</span>';
    return `<tr>
      <td>${esc(s.label)}</td>
      <td>${esc(s.proxy || '-')}</td>
      <td>${fmtWon(s.exposure_krw)}</td>
      <td>${fmtPct(s.exposure_pct)}</td>
      <td>${holdings}</td>
    </tr>`;
  }).join('');
  const unmapped = data.unmapped || [];
  $('sectorUnmapped').innerHTML = data.configured && unmapped.length
    ? `섹터에 연결하지 않은 보유종목 ${unmapped.length}개: ${unmapped.slice(0, 8).map(u => esc(u.name)).join(', ')}${unmapped.length > 8 ? ' 외' : ''}`
    : '';
}

async function loadSectorExposure() {
  const { ok, data } = await getJson('/api/sector-exposure');
  if (ok) renderSectorExposure(data);
}

// ---------- 설정 편집 ----------
let sectorDraft = { sectors: [], assignments: [] };
const sectorNames = {}; // 검색으로 고른 종목의 이름(표시용). 저장되지는 않는다.

function setSectorDraft(data) {
  sectorDraft = {
    sectors: (data.sectors || []).map(s => ({ label: s.label, proxy: s.proxy || '', ...(s.toss_id ? { toss_id: s.toss_id } : {}) })),
    assignments: (data.assignments || []).map(a => ({ symbol: a.symbol, sector: a.sector, pct: a.pct })),
  };
  renderSectorSettings();
}

function renderSectorList() {
  const container = $('sectorList');
  container.innerHTML = '';
  sectorDraft.sectors.forEach((s, i) => {
    const row = document.createElement('div');
    row.className = 'sector-row';
    row.innerHTML = `
      <input type="text" placeholder="섹터 이름 (예: 반도체)" value="${esc(s.label)}" data-field="label">
      <input type="text" placeholder="대표 티커 (선택, 예: SOXX)" value="${esc(s.proxy)}" data-field="proxy">
      <button type="button" class="rb-remove">삭제</button>`;
    const field = name => row.querySelector(`[data-field="${name}"]`);
    field('label').addEventListener('input', e => {
      const old = s.label;
      s.label = e.target.value;
      // 이름을 고치면 이 섹터를 가리키던 배정도 같이 따라간다(키 입력마다 이전 값 → 새 값).
      sectorDraft.assignments.forEach(a => { if (a.sector === old) a.sector = s.label; });
      renderSectorAssignments();
    });
    field('proxy').addEventListener('input', e => { s.proxy = e.target.value.trim().toUpperCase(); });
    row.querySelector('.rb-remove').addEventListener('click', () => {
      sectorDraft.assignments = sectorDraft.assignments.filter(a => a.sector !== s.label);
      sectorDraft.sectors.splice(i, 1);
      renderSectorSettings();
    });
    container.appendChild(row);
  });
}

function renderSectorAssignments() {
  const container = $('sectorAssignments');
  container.innerHTML = '';
  sectorDraft.assignments.forEach((a, i) => {
    const row = document.createElement('div');
    row.className = 'sector-assign-row';
    const options = sectorDraft.sectors
      .map(s => `<option value="${esc(s.label)}"${s.label === a.sector ? ' selected' : ''}>${esc(s.label || '(이름 없음)')}</option>`)
      .join('');
    row.innerHTML = `
      <div class="sector-assign-name">${nameWithSymbol(sectorNames[a.symbol] || a.symbol, a.symbol)}</div>
      <select data-field="sector">${options}</select>
      <input type="number" min="0" max="100" step="1" value="${a.pct}" data-field="pct" title="노출% (이 종목이 이 섹터에 해당하는 비율)">
      <button type="button" class="rb-remove">삭제</button>`;
    row.querySelector('[data-field="sector"]').addEventListener('change', e => { a.sector = e.target.value; });
    row.querySelector('[data-field="pct"]').addEventListener('input', e => { a.pct = parseFloat(e.target.value) || 0; });
    row.querySelector('.rb-remove').addEventListener('click', () => {
      sectorDraft.assignments.splice(i, 1);
      renderSectorAssignments();
    });
    container.appendChild(row);
  });
}

function renderSectorSettings() {
  renderSectorList();
  renderSectorAssignments();
}

async function loadSectors() {
  const [map] = await Promise.all([getJson('/api/sector-map'), loadSectorExposure()]);
  if (map.ok) setSectorDraft(map.data);
}

$('addSectorBtn').addEventListener('click', () => {
  sectorDraft.sectors.push({ label: '', proxy: '' });
  renderSectorSettings();
});

attachSymbolSearch({
  input: $('sectorSearchInput'),
  suggestions: $('sectorSuggestions'),
  onPick: r => {
    if (!sectorDraft.sectors.length) {
      showMsg($('sectorMapError'), '먼저 섹터를 하나 이상 만드세요.');
      return;
    }
    hideEl($('sectorMapError'));
    sectorNames[r.symbol] = r.name;
    // 새 배정의 기본 섹터는 첫 번째 섹터, 노출%는 100. 같은 종목·섹터 조합이 이미 있으면 다른 섹터로 넘긴다.
    const taken = new Set(sectorDraft.assignments.filter(a => a.symbol === r.symbol).map(a => a.sector));
    const target = sectorDraft.sectors.find(s => !taken.has(s.label)) || sectorDraft.sectors[0];
    if (taken.has(target.label)) {
      showMsg($('sectorMapError'), '이 종목은 모든 섹터에 이미 배정되어 있습니다.');
      return;
    }
    sectorDraft.assignments.push({ symbol: r.symbol, sector: target.label, pct: 100 });
    renderSectorAssignments();
  },
  tagFor: r => (sectorDraft.assignments.some(a => a.symbol === r.symbol) ? ' · 배정됨' : ''),
});

$('saveSectorMapBtn').addEventListener('click', () => runSave({
  btn: $('saveSectorMapBtn'), errEl: $('sectorMapError'), savedEl: $('sectorMapSaved'), url: '/api/sector-map',
  body: sectorDraft,
  onSuccess: async data => {
    setSectorDraft(data);
    await loadSectorExposure(); // 저장한 매핑을 노출 표에 바로 반영
  },
}));
$('resetSectorMapBtn').addEventListener('click', loadSectors);

// ---------- 토스 분류로 자동 채우기 ----------
// 서버가 토스 섹터 API로 "종목 → 소속 섹터" 색인을 모아 두면(백그라운드, 하루 한 번), 보유 종목마다 후보를 보여준다.
// 후보는 초안에 추가만 하고 저장은 사용자가 따로 누른다.
let autoSuggestions = [];
const autoEdits = {}; // `${symbol}|${sector_id}` → { checked, pct } (다시 그려도 사용자가 고친 값을 유지)
let autoTimer = null;

function autoKey(symbol, sectorId) { return `${symbol}|${sectorId}`; }

function autoDefaults(s) {
  // 주식: 모든 후보를 선택하고 노출%를 균등 분할(한 종목의 합계 100 이하). ETF: 선택·비율 모두 비워 둔다.
  const n = s.candidates.length || 1;
  return s.kind === 'stock'
    ? { checked: true, pct: Math.floor(1000 / n) / 10 }
    : { checked: false, pct: '' };
}

function renderAutoRows() {
  const box = $('sectorAutoRows');
  box.innerHTML = '';
  const withCandidates = autoSuggestions.filter(s => s.candidates.length);
  autoSuggestions.forEach(s => {
    const row = document.createElement('div');
    row.className = 'sector-auto-row';
    const kindLabel = s.kind === 'stock' ? '주식' : s.kind === 'etf' ? 'ETF' : '';
    let body;
    if (!s.candidates.length) {
      body = s.pending ? '<span class="muted">분류를 찾는 중…</span>' : '<span class="muted">토스 분류 없음 — 직접 입력하세요</span>';
    } else {
      body = s.candidates.map(c => {
        const key = autoKey(s.symbol, c.sector_id);
        if (!autoEdits[key]) autoEdits[key] = autoDefaults(s);
        const e = autoEdits[key];
        const depth = ['대분류', '중분류', '소분류'][c.depth] || '';
        return `<label class="sector-auto-cand">
          <input type="checkbox" data-key="${esc(key)}" ${e.checked ? 'checked' : ''}>
          ${esc(c.name)} <span class="muted">${esc(depth)}</span>
          <input type="number" min="0" max="100" step="1" placeholder="노출%" value="${esc(e.pct)}" data-pct="${esc(key)}">
        </label>`;
      }).join('');
    }
    row.innerHTML = `<div class="sector-auto-name">${nameWithSymbol(s.name, s.symbol)} <span class="muted">${esc(kindLabel)}</span></div><div>${body}</div>`;
    row.querySelectorAll('input[type="checkbox"]').forEach(el =>
      el.addEventListener('change', ev => { autoEdits[ev.target.dataset.key].checked = ev.target.checked; }));
    row.querySelectorAll('input[data-pct]').forEach(el =>
      el.addEventListener('input', ev => { autoEdits[ev.target.dataset.pct].pct = ev.target.value; }));
    box.appendChild(row);
  });
  $('sectorAutoActions').style.display = withCandidates.length ? 'flex' : 'none';
}

function describeAutoStatus(st) {
  if (st.state === 'running') return `토스 섹터 정보 수집 중… ${st.done}/${st.total}`;
  if (st.state === 'error') return st.message || '수집 중 오류가 났습니다.';
  if (!st.total || !st.done) return '';
  if (st.stocks_done && !st.etfs_done) return `주식 분류는 준비됐고 ETF 분류는 ${st.done}/${st.total}까지 수집했습니다. 다시 누르면 이어서 진행합니다.`;
  if (st.stocks_done && st.etfs_done) return '수집 완료 (하루 한 번 갱신)';
  return `수집이 중단되었습니다 (${st.done}/${st.total}). 다시 누르면 이어서 진행합니다.`;
}

async function pollAutoSuggestions() {
  clearTimeout(autoTimer);
  const { ok, data } = await getJson('/api/sector-suggestions');
  if (!ok) { showMsg($('sectorAutoError'), data.error || '제안을 불러오지 못했습니다.'); return; }
  autoSuggestions = data.suggestions || [];
  $('sectorAutoStatus').textContent = describeAutoStatus(data.status);
  renderAutoRows();
  if (data.status.state === 'running') autoTimer = setTimeout(pollAutoSuggestions, 2000);
}

$('sectorAutoBtn').addEventListener('click', async () => {
  const btn = $('sectorAutoBtn');
  hideEl($('sectorAutoError'));
  btn.disabled = true;
  try {
    const { ok, data } = await postJson('/api/sector-index/refresh', {});
    if (!ok) { showMsg($('sectorAutoError'), data.error || '시작하지 못했습니다.'); return; }
    await pollAutoSuggestions();
  } catch (e) {
    showMsg($('sectorAutoError'), '서버에 연결할 수 없습니다.');
  } finally {
    btn.disabled = false;
  }
});

$('applySectorAutoBtn').addEventListener('click', () => {
  hideEl($('sectorAutoError'));
  let added = 0, skipped = 0;
  autoSuggestions.forEach(s => s.candidates.forEach(c => {
    const e = autoEdits[autoKey(s.symbol, c.sector_id)];
    if (!e || !e.checked) return;
    const pct = parseFloat(e.pct);
    if (!(pct > 0 && pct <= 100)) { skipped++; return; }
    // 같은 토스 섹터(또는 같은 이름)가 이미 있으면 재사용하고, 없으면 새로 만든다.
    let sector = sectorDraft.sectors.find(x => x.toss_id === c.sector_id) || sectorDraft.sectors.find(x => x.label === c.name);
    if (!sector) {
      sector = { label: c.name, proxy: '', toss_id: c.sector_id };
      sectorDraft.sectors.push(sector);
    } else if (!sector.toss_id) {
      sector.toss_id = c.sector_id;
    }
    if (sectorDraft.assignments.some(a => a.symbol === s.symbol && a.sector === sector.label)) { skipped++; return; }
    sectorNames[s.symbol] = s.name;
    sectorDraft.assignments.push({ symbol: s.symbol, sector: sector.label, pct });
    added++;
  }));
  renderSectorSettings();
  $('sectorAutoApplied').textContent = `${added}개를 초안에 추가했습니다${skipped ? ` (비율이 없거나 이미 있는 ${skipped}개는 건너뜀)` : ''}. 아래 저장을 눌러야 반영됩니다.`;
});
