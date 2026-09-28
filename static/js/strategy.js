// 분할매수 전략: 템플릿 편집(지표 자유 조합), 종목별 배정·기본 전략·목표 비중, 결과 표.
// "MA 4단계 하방돌파 → 회복"이라는 특정 모양을 하드코딩하지 않고, 지표(MA/RSI/등락률)를 섞어 "이름 붙은
// 전략 템플릿"을 만들고 종목마다 템플릿을 배정하는 구조.

const STRATEGY_INDICATOR_LABELS = { MA: '이동평균', RSI: 'RSI', CHANGE: '등락률' };
// 지표를 잘 모르는 사용자도 화면에서 바로 이해할 수 있도록 쉬운 말로 설명(배포 후엔 화면 설명이 유일한 안내).
const STRATEGY_INDICATOR_HELP = {
  MA: '최근 N일/N주 평균 가격보다 지금 가격이 낮아지면 매수 신호로 봅니다. 기간이 길수록(예: 240주) 더 크게 하락했을 때만 신호가 뜹니다.',
  RSI: '최근 상승폭 대비 하락폭 비율(0~100)입니다. 값이 낮을수록 "많이 팔려서 과매도"인 상태로 보고, 임계값 이하로 내려가면 매수 신호로 봅니다(보통 30 이하를 과매도로 봄).',
  CHANGE: '전일 대비 하루 동안 오른 정도입니다. 음수를 기준으로 정하면(예: -10) 하루에 그만큼 이상 급락했을 때 매수 신호로 봅니다.',
};
const STRATEGY_INDICATOR_DEFAULTS = {
  MA: { interval: 'week', period: 60 },
  RSI: { period: 14, threshold: 30 },
  CHANGE: { threshold_pct: -10 },
};
const STRATEGY_ASSIGN_MODE_HELP = {
  fixed: '각 단계는 자기 몫의 비율을 고정으로 갖습니다 (예: RSI 단계는 항상 10%, 등락률 단계는 항상 20%). 서로 다른 지표를 섞을 때 적합합니다.',
  order: '어떤 단계가 몇 번째로 조건을 만족했는지"만" 보고, 그 순번에 정해둔 비율을 부여합니다(어떤 지표인지는 안 따짐 - 예: 이동평균 여러 개가 꼬여서 순서가 뒤바뀌어도 "1번째로 닿은 것"이면 동일하게 취급). real/에서 쓰던 원래 방식이 이거예요.',
};
const STRATEGY_INDICATOR_FIELDS = {
  MA: [
    { key: 'interval', label: '봉', type: 'select', options: [['week', '주봉'], ['day', '일봉']] },
    { key: 'period', label: '기간', type: 'number', min: 2, max: 300, step: 1 },
  ],
  RSI: [
    { key: 'period', label: '기간(일)', type: 'number', min: 2, max: 200, step: 1 },
    { key: 'threshold', label: '임계값 이하', type: 'number', min: 0, max: 100, step: 0.1 },
  ],
  CHANGE: [
    { key: 'threshold_pct', label: '등락률(%) 이하', type: 'number', min: -100, max: 100, step: 0.1 },
  ],
};
const STEP_LISTS = [ // 에디터의 세 단계 목록: [draft 키, 컨테이너 id, 매수%p 입력 여부, 새로 추가할 때 기본 지표]
  ['steps', 'strategyTplSteps', true, 'MA'],
  ['recovery_steps', 'strategyTplRecoverySteps', false, 'MA'],
  ['multiplier_steps', 'strategyTplMultiplierSteps', false, 'RSI'],
];

let strategyTemplates = {};       // 서버와 동기화된 템플릿들 {name: {label, steps, recovery_steps, ...}}
let strategyAssignments = {};     // {symbol: templateName} - 직접 배정
let strategyPositionPct = {};     // {symbol: 최종 목표 비중(총자산 대비 %)}
let strategyDefaultTemplate = ''; // 직접 배정 안 된 보유종목에 자동 적용할 템플릿('' = 없음)
let strategyAutoSymbols = [];     // 마지막 계산에서 기본 전략으로 자동 배정된 종목들
let strategyEditingName = null;   // 에디터에 열린 템플릿의 원래 이름(신규면 null)
let strategyEditingDraft = null;  // 에디터에서 편집 중인 단계 목록들
let strategySelectedSymbol = null; // 배정용 검색으로 고른 종목 {symbol, name}

const strategyError = msg => showMsg($('strategyError'), msg);
const newStrategyStep = indicator => ({ id: null, indicator, params: { ...STRATEGY_INDICATOR_DEFAULTS[indicator] }, buy_pct: 5 });
const copySteps = steps => (steps || []).map(s => ({ ...s, params: { ...s.params } }));
const templateOptions = names => names.map(n => `<option value="${esc(n)}">${esc(strategyTemplates[n].label)}</option>`).join('');

// ---------- 템플릿 목록 / 드롭다운 ----------
function renderStrategyTemplateList() {
  const container = $('strategyTemplateList');
  const names = Object.keys(strategyTemplates);
  container.innerHTML = names.length ? '' : '<div class="muted" style="font-size:13px;">등록된 템플릿이 없습니다. "+ 새 템플릿"으로 만드세요.</div>';
  names.forEach(name => {
    const tpl = strategyTemplates[name];
    const row = document.createElement('div');
    row.className = 'strategy-template-row' + (name === strategyEditingName ? ' active' : '');
    row.innerHTML = `<span>${esc(tpl.label)}</span><span class="meta">${esc(name)} · 단계 ${tpl.steps.length}개 · ${tpl.assign_mode === 'order' ? '순서 배정' : '고정 배정'}</span>`;
    row.addEventListener('click', () => openStrategyTemplateEditor(name));
    container.appendChild(row);
  });
  $('strategyAssignTemplateSelect').innerHTML = templateOptions(names);
  const defSelect = $('strategyDefaultTemplateSelect');
  defSelect.innerHTML = '<option value="">자동 적용 안 함</option>' + templateOptions(names);
  defSelect.value = strategyTemplates[strategyDefaultTemplate] ? strategyDefaultTemplate : '';
}
$('strategyDefaultTemplateSelect').addEventListener('change', e => { strategyDefaultTemplate = e.target.value; });

// ---------- 템플릿 에디터 ----------
function updateStrategyAssignModeHelp() {
  $('strategyTplAssignModeHelp').textContent = STRATEGY_ASSIGN_MODE_HELP[$('strategyTplAssignModeSelect').value];
}
$('strategyTplAssignModeSelect').addEventListener('change', updateStrategyAssignModeHelp);

function showStrategyEditor(name, tpl) {
  strategyEditingName = name;
  strategyEditingDraft = {
    steps: copySteps(tpl.steps), recovery_steps: copySteps(tpl.recovery_steps), multiplier_steps: copySteps(tpl.multiplier_steps),
    cycle_steps: (tpl.cycle_steps || []).map(r => ({ ...r })),
  };
  $('strategyTplCycleEnabled').checked = strategyEditingDraft.cycle_steps.length > 0;
  $('strategyTplNameInput').value = name || '';
  $('strategyTplNameInput').disabled = !!name; // 기존 템플릿은 이름(식별자) 변경 불가(단순화)
  $('strategyTplLabelInput').value = tpl.label || '';
  $('strategyTplAssignModeSelect').value = tpl.assign_mode || 'fixed';
  $('strategyTplRecoveryPctInput').value = tpl.recovery_pct || 0;
  $('strategyTplMultiplierFactorInput').value = tpl.multiplier_factor || 1;
  $('strategyTemplateEditor').style.display = 'block';
  $('deleteStrategyTemplateBtn').style.display = name ? 'inline-block' : 'none';
  updateStrategyAssignModeHelp();
  renderStrategyTplSteps();
  renderStrategyTemplateList();
}

function closeStrategyEditor() {
  strategyEditingName = null;
  strategyEditingDraft = null;
  hideEl($('strategyTemplateEditor'));
  renderStrategyTemplateList();
}

const openStrategyTemplateEditor = name => showStrategyEditor(name, strategyTemplates[name]);
$('addStrategyTemplateBtn').addEventListener('click', () => showStrategyEditor(null, { steps: [newStrategyStep('MA')] }));
$('cancelStrategyTemplateBtn').addEventListener('click', closeStrategyEditor);

function renderStrategyStepParams(container, step, onChange) {
  container.innerHTML = '';
  STRATEGY_INDICATOR_FIELDS[step.indicator].forEach(f => {
    const label = document.createElement('label');
    label.innerHTML = f.type === 'select'
      ? `${f.label} <select data-key="${f.key}">${f.options.map(([v, l]) => `<option value="${v}" ${step.params[f.key] === v ? 'selected' : ''}>${l}</option>`).join('')}</select>`
      : `${f.label} <input type="number" data-key="${f.key}" value="${step.params[f.key]}" min="${f.min}" max="${f.max}" step="${f.step}">`;
    label.querySelector('[data-key]').addEventListener('input', e => {
      step.params[f.key] = f.type === 'select' ? e.target.value : (parseFloat(e.target.value) || 0);
      onChange();
    });
    container.appendChild(label);
  });
}

function renderStrategyStepList(containerId, steps, withBuyPct) {
  const container = $(containerId);
  container.innerHTML = '';
  steps.forEach((step, i) => {
    const row = document.createElement('div');
    row.className = 'strategy-step-row';
    const indicatorOptions = Object.entries(STRATEGY_INDICATOR_LABELS)
      .map(([k, l]) => `<option value="${k}" ${k === step.indicator ? 'selected' : ''}>${l}</option>`).join('');
    row.innerHTML = `
      <select class="indicator-select">${indicatorOptions}</select>
      <div class="params"></div>
      ${withBuyPct ? `<input type="number" class="buy-pct" min="0.1" max="100" step="0.1" value="${step.buy_pct}" placeholder="매수%p">` : ''}
      <button type="button" class="rb-remove">삭제</button>
      <div class="help">${STRATEGY_INDICATOR_HELP[step.indicator]}</div>`;
    renderStrategyStepParams(row.querySelector('.params'), step, updateStrategyTplSum);
    row.querySelector('.indicator-select').addEventListener('change', e => {
      step.indicator = e.target.value;
      step.params = { ...STRATEGY_INDICATOR_DEFAULTS[step.indicator] };
      renderStrategyStepParams(row.querySelector('.params'), step, updateStrategyTplSum);
      row.querySelector('.help').textContent = STRATEGY_INDICATOR_HELP[step.indicator];
    });
    if (withBuyPct) {
      row.querySelector('.buy-pct').addEventListener('input', e => {
        step.buy_pct = parseFloat(e.target.value) || 0;
        updateStrategyTplSum();
      });
    }
    row.querySelector('.rb-remove').addEventListener('click', () => {
      steps.splice(i, 1);
      renderStrategyTplSteps();
    });
    container.appendChild(row);
  });
}

function renderStrategyTplSteps() {
  STEP_LISTS.forEach(([key, id, withBuyPct]) => renderStrategyStepList(id, strategyEditingDraft[key], withBuyPct));
  renderStrategyCycleSteps();
  updateStrategyTplSum();
}

// ---- 2차 이후 사이클 표: 매수 단계 수만큼 "k번째 돌파 → 매도%p / 매수%p" ----
function renderStrategyCycleSteps() {
  const draft = strategyEditingDraft;
  const enabled = $('strategyTplCycleEnabled').checked;
  const n = draft.steps.length;
  // 매수 단계 수가 바뀌면 표 길이를 맞춘다(새 칸은 0으로). 껐다 켜도 입력값이 남도록 꺼질 때 보관해 둔다.
  if (enabled) {
    const prev = draft.cycle_steps.length ? draft.cycle_steps : (draft.cycle_stash || []);
    draft.cycle_steps = Array.from({ length: n }, (_, i) => prev[i] || { sell_pct: 0, buy_pct: 0 });
  } else {
    if (draft.cycle_steps.length) draft.cycle_stash = draft.cycle_steps;
    draft.cycle_steps = [];
  }
  const box = $('strategyTplCycleSteps');
  box.innerHTML = '';
  draft.cycle_steps.forEach((row, i) => {
    const el = document.createElement('div');
    el.className = 'strategy-cycle-row';
    el.innerHTML = `<span style="min-width:70px;">${i + 1}번째 돌파</span>
      매도 <input type="number" min="0" max="100" step="0.1" value="${row.sell_pct}" data-k="sell_pct">%p
      매수 <input type="number" min="0" max="100" step="0.1" value="${row.buy_pct}" data-k="buy_pct">%p`;
    el.querySelectorAll('input').forEach(inp => inp.addEventListener('input', () => {
      row[inp.dataset.k] = parseFloat(inp.value) || 0;
      updateStrategyCyclePath();
    }));
    box.appendChild(el);
  });
  updateStrategyCyclePath();
}

function updateStrategyCyclePath() {
  const el = $('strategyTplCyclePath');
  const rows = strategyEditingDraft.cycle_steps;
  if (!rows.length) { el.textContent = ''; return; }
  let level = 100;
  const path = [100];
  rows.forEach(r => { level += r.buy_pct - r.sell_pct; path.push(Math.round(level * 10) / 10); });
  const recovery = parseFloat($('strategyTplRecoveryPctInput').value) || 0;
  const end = Math.min(100, level + recovery);
  el.textContent = `n차 사이클 흐름: ${path.join('% → ')}% → 회복 +${recovery}%p → ${Math.round(end * 10) / 10}%`;
  el.classList.toggle('warn', path.some(v => v < 0));
}
$('strategyTplCycleEnabled').addEventListener('change', renderStrategyCycleSteps);

function updateStrategyTplSum() {
  const stepSum = strategyEditingDraft.steps.reduce((s, st) => s + (parseFloat(st.buy_pct) || 0), 0);
  const recoveryPct = parseFloat($('strategyTplRecoveryPctInput').value) || 0;
  const total = Math.round((stepSum + recoveryPct) * 10) / 10;
  const el = $('strategyTplSum');
  el.textContent = `단계별 매수비율(%p) 합계 ${Math.round(stepSum * 10) / 10}% + 회복 매수 ${recoveryPct}% = ${total}% (100%면 목표 진입 완료)`;
  el.classList.toggle('warn', total > 100);
}

$('strategyTplRecoveryPctInput').addEventListener('input', () => { updateStrategyTplSum(); updateStrategyCyclePath(); });
[['addStrategyStepBtn', 0], ['addStrategyRecoveryStepBtn', 1], ['addStrategyMultiplierStepBtn', 2]].forEach(([btnId, i]) => {
  $(btnId).addEventListener('click', () => {
    const [key, , , indicator] = STEP_LISTS[i];
    strategyEditingDraft[key].push(newStrategyStep(indicator));
    renderStrategyTplSteps();
  });
});

$('saveStrategyTemplateBtn').addEventListener('click', () => {
  hideEl($('strategyError'));
  const name = $('strategyTplNameInput').value.trim();
  if (!/^[A-Za-z0-9_-]{1,30}$/.test(name)) return strategyError('템플릿 이름은 영문/숫자/-_ 조합 1~30자여야 합니다.');
  if (name !== strategyEditingName && strategyTemplates[name]) return strategyError('이미 있는 템플릿 이름입니다.');
  const strip = steps => steps.map(({ id, indicator, params }) => ({ id, indicator, params: { ...params } }));
  strategyTemplates[name] = {
    label: $('strategyTplLabelInput').value.trim() || name,
    assign_mode: $('strategyTplAssignModeSelect').value,
    steps: strategyEditingDraft.steps.map(({ id, indicator, params, buy_pct }) => ({ id, indicator, params: { ...params }, buy_pct })),
    recovery_steps: strip(strategyEditingDraft.recovery_steps),
    recovery_pct: parseFloat($('strategyTplRecoveryPctInput').value) || 0,
    multiplier_steps: strip(strategyEditingDraft.multiplier_steps),
    multiplier_factor: parseFloat($('strategyTplMultiplierFactorInput').value) || 1,
    cycle_steps: strategyEditingDraft.cycle_steps.map(({ sell_pct, buy_pct }) => ({ sell_pct, buy_pct })),
  };
  closeStrategyEditor();
  renderStrategyAssignList();
});

$('deleteStrategyTemplateBtn').addEventListener('click', () => {
  if (!strategyEditingName) return;
  if (Object.values(strategyAssignments).includes(strategyEditingName)) {
    return strategyError('이 템플릿을 쓰는 종목이 있어서 삭제할 수 없습니다. 먼저 배정을 해제하세요.');
  }
  if (strategyDefaultTemplate === strategyEditingName) {
    return strategyError('기본 전략으로 쓰는 템플릿이라 삭제할 수 없습니다. 먼저 기본 전략을 바꾸세요.');
  }
  delete strategyTemplates[strategyEditingName];
  closeStrategyEditor();
});

// ---------- 종목별 배정 ----------
attachSymbolSearch({
  input: $('strategyAssignSearchInput'),
  suggestions: $('strategyAssignSuggestions'),
  onPick: r => {
    strategySelectedSymbol = { symbol: r.symbol, name: r.name };
    $('strategyAssignSelected').textContent = `선택된 종목: ${r.name} (${r.symbol})`;
  },
});

function renderStrategyAssignList() {
  const container = $('strategyAssignList');
  const explicit = Object.keys(strategyAssignments);
  const auto = strategyAutoSymbols.filter(sym => !(sym in strategyAssignments));
  container.innerHTML = explicit.length || auto.length ? '' : '<div class="muted" style="font-size:13px;">배정된 종목이 없습니다.</div>';
  const addRow = (sym, metaText, btnText, onRemove) => {
    const row = document.createElement('div');
    row.className = 'strategy-assign-row';
    row.innerHTML = `<span>${esc(sym)}</span><span class="meta">${esc(metaText)}</span>
      <input type="number" class="pos-pct" min="0" max="100" step="0.1" placeholder="목표%" title="최종 목표 비중(총자산 대비 %)">
      <button type="button" class="rb-remove">${btnText}</button>`;
    const posInput = row.querySelector('.pos-pct');
    posInput.value = strategyPositionPct[sym] ?? '';
    posInput.addEventListener('input', () => {
      if (posInput.value === '') delete strategyPositionPct[sym];
      else strategyPositionPct[sym] = Number(posInput.value);
    });
    row.querySelector('.rb-remove').addEventListener('click', onRemove);
    container.appendChild(row);
  };
  explicit.forEach(sym => {
    const tplName = strategyAssignments[sym];
    addRow(sym, strategyTemplates[tplName]?.label ?? tplName, '해제', () => {
      delete strategyAssignments[sym];
      if (!strategyDefaultTemplate) delete strategyPositionPct[sym]; // 기본 전략이 있으면 보유종목은 다시 자동 배정됨
      renderStrategyAssignList();
    });
  });
  auto.forEach(sym => {
    addRow(sym, `기본 · ${strategyTemplates[strategyDefaultTemplate]?.label ?? strategyDefaultTemplate}`, '제외', () => {
      const list = parseSymbolList($('strategyExcludedInput').value);
      if (!list.includes(sym)) list.push(sym);
      $('strategyExcludedInput').value = list.join(', ');
      delete strategyPositionPct[sym];
      strategyAutoSymbols = strategyAutoSymbols.filter(v => v !== sym);
      renderStrategyAssignList();
    });
  });
}

$('addStrategyAssignBtn').addEventListener('click', () => {
  hideEl($('strategyError'));
  if (!strategySelectedSymbol) return strategyError('먼저 종목을 검색해서 선택하세요.');
  const tplName = $('strategyAssignTemplateSelect').value;
  if (!tplName) return strategyError('배정할 템플릿이 없습니다. 먼저 템플릿을 만드세요.');
  strategyAssignments[strategySelectedSymbol.symbol] = tplName;
  strategySelectedSymbol = null;
  $('strategyAssignSelected').textContent = '검색해서 종목을 먼저 선택하세요.';
  renderStrategyAssignList();
});

// ---------- 결과 표 ----------
function strategyWeightCell(r) {
  const z = r.sizing;
  if (!z) return '<td class="muted">목표% 미설정</td>';
  return `<td>${z.current_pct.toFixed(2)}% → <b>${z.target_now_pct.toFixed(2)}%</b>` +
    `<span class="strategy-sub">최종 ${z.position_pct.toFixed(1)}% (${fmtWon(z.full_krw)})</span></td>`;
}

// 매도 신호 옆 예상 세금(세금 페이지와 같은 계산, 판 금액에 비례). 0이면 표시 안 함.
const taxNote = krw => (krw > 0 ? ` · 세금 약 ${fmtWon(krw)}` : '');

function strategyBuyCell(r) {
  const z = r.sizing;
  if (!z) return '<td class="muted">–</td>';
  const shares = n => (n > 0 ? ` · 약 ${n.toLocaleString('ko-KR')}주` : '');
  let now = '<span class="muted">지금 없음</span>';
  if (z.buy_now_krw > 0) now = `지금 매수 <b>${fmtWon(z.buy_now_krw)}</b>${shares(z.buy_now_shares)}`;
  else if (z.over_krw > 0 && r.cycle >= 2) now = `지금 매도 <b class="loss">${fmtWon(z.over_krw)}</b>${shares(z.over_shares)}${taxNote(z.sell_now_tax_krw)}`;
  else if (z.over_krw > 0) now += `<span class="strategy-sub">목표보다 ${fmtWon(z.over_krw)} 많음</span>`;
  const change = z.next_change_krw;
  const next = change > 0 ? `다음 단계 매수 ${fmtWon(change)}${shares(z.next_change_shares)}`
    : change < 0 ? `다음 단계 매도 ${fmtWon(-change)}${shares(z.next_change_shares)}${taxNote(z.next_sell_tax_krw)}` : '다음 단계 없음 (완료)';
  return `<td>${now}<span class="strategy-sub">${next}</span></td>`;
}

function strategyNextText(r) {
  if (r.next_sell_pct > 0) return `다음 −${r.next_sell_pct.toFixed(1)} +${r.next_buy_pct.toFixed(1)}%p`;
  return r.next_buy_pct > 0 ? `다음 +${r.next_buy_pct.toFixed(1)}%p` : '완료';
}

function strategyBadges(r) {
  const badge = (text, on) => `<span class="strategy-stage-badge${on ? ' broken' : ''}">${esc(text)}</span>`;
  let html = r.steps.map(s => (s.ok ? badge(s.label, s.broken) : badge('데이터부족', false))).join('');
  if (r.recovered) html += badge('회복 완료 ✅', true);
  else if (r.stage_count >= r.stage_total) html += badge('회복 대기중', false);
  if (r.multiplier_triggered) html += badge(`배수 x${r.multiplier_factor} 발동`, true);
  return `<div class="strategy-badges">${html}</div>`;
}

function renderStrategyResults(rows, totalKrw) {
  $('strategyTotal').textContent = totalKrw
    ? `총자산(보유종목 + 예수금) ${fmtWon(totalKrw)} 기준 · 지금 목표 = 최종 목표 비중 × 진입 목표비율 · 주수는 현재가 기준 내림` : '';
  const tbody = $('strategyRows');
  tbody.innerHTML = rows.length ? '' : '<tr class="empty-row"><td colspan="4">대상 종목이 없습니다(전략이 배정된 보유·관심종목이 없음).</td></tr>';
  rows.forEach(r => {
    const tr = document.createElement('tr');
    const head = `${nameWithSymbol(r.name, r.symbol)}<span class="strategy-sub">${esc(r.template_label)}</span>`;
    if (!r.ok) {
      tr.innerHTML = `<td style="text-align:left;">${head}</td><td colspan="3" class="muted" style="text-align:left;">${esc(r.reason)}</td>`;
    } else {
      tr.innerHTML = `
        <td style="text-align:left;">${head}${strategyBadges(r)}</td>
        <td style="white-space:nowrap;">${r.cycle}차 · ${r.stage_count}/${r.stage_total}단계<br><b>${r.entry_pct.toFixed(1)}%</b>
          <span class="strategy-sub">${strategyNextText(r)}</span></td>
        ${strategyWeightCell(r)}
        ${strategyBuyCell(r)}`;
    }
    tbody.appendChild(tr);
  });
}

// ---------- 불러오기 / 저장 ----------
async function loadStrategyConfig() {
  const { ok, data } = await getJson('/api/strategy-templates');
  if (!ok) return;
  strategyTemplates = data.templates || {};
  strategyAssignments = data.assignments || {};
  strategyPositionPct = data.position_pct || {};
  strategyDefaultTemplate = data.default_template || '';
  $('strategyExcludedInput').value = (data.excluded_symbols || []).join(', ');
  closeStrategyEditor();
  renderStrategyAssignList();
}

async function loadStrategyRows() {
  const { ok, data } = await getJson('/api/strategy');
  if (!ok) return;
  strategyAutoSymbols = data.auto_assigned || [];
  renderStrategyAssignList();
  renderStrategyResults(data.rows || [], data.total_krw);
}

async function loadStrategy() {
  await loadStrategyConfig();
  await loadStrategyRows();
}

$('resetStrategyConfigBtn').addEventListener('click', loadStrategyConfig);
$('saveStrategyConfigBtn').addEventListener('click', () => runSave({
  btn: $('saveStrategyConfigBtn'), errEl: $('strategyError'), savedEl: $('strategyConfigSaved'), url: '/api/strategy-templates',
  body: {
    templates: strategyTemplates,
    assignments: strategyAssignments,
    excluded_symbols: parseSymbolList($('strategyExcludedInput').value),
    default_template: strategyDefaultTemplate,
    position_pct: strategyPositionPct,
  },
  onSuccess: loadStrategy,
}));
