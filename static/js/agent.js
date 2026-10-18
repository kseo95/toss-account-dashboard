// Claude 도구 호출 Agent. 대화 기록은 브라우저 메모리에만 두고 서버에는 최근 문맥만 보낸다.
let agentHistory = [];

function renderAgentList(id, items) {
  // 빈 문자열은 항목 사이 간격으로 그린다(답변을 구역으로 나눌 때 쓴다).
  $(id).innerHTML = (items || []).map(item => item === '' ? '<li class="agent-gap"></li>' : '<li>' + esc(item) + '</li>').join('');
}

// 못 알아들은 질문 뒤에 보여 주는 질문 예시 버튼(내 종목 이름이 들어간다). 글자는 textContent로만 넣는다.
function renderAgentSuggestions(items) {
  const box = $('agentSuggestions');
  box.innerHTML = '';
  (items || []).forEach(question => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = question;
    button.addEventListener('click', () => {
      $('agentQuestion').value = question;
      runAgentQuestion();
    });
    box.appendChild(button);
  });
  box.style.display = box.children.length ? '' : 'none';
}

// 못 알아들은 질문을 이어서 물어본 질문과 자동으로 연결했을 때 알리고 취소할 수 있게 한다.
function renderAgentLearned(learned) {
  const box = $('agentLearned');
  if (!learned) { box.style.display = 'none'; return; }
  $('agentLearnedText').textContent = '방금 전 질문 "' + learned.question + '"을(를) "' + learned.label + '"의 다른 표현으로 기억했어요.';
  $('agentLearnedUndo').onclick = async () => {
    const { ok } = await postJson('/api/agent/teach', { forget: learned.question });
    $('agentLearnedText').textContent = ok ? '취소했어요. 기억하지 않습니다.' : '취소하지 못했습니다.';
    $('agentLearnedUndo').style.display = 'none';
  };
  $('agentLearnedUndo').style.display = '';
  box.style.display = '';
}

// 놓친 질문 기록 설정(기본 꺼짐)
async function loadAgentMisses() {
  try {
    const { ok, data } = await getJson('/api/agent/misses');
    if (!ok) return;
    $('agentMissEnable').checked = !!data.enabled;
    $('agentMissCount').textContent = '기록 ' + data.count + '건';
  } catch (e) { /* 다음에 다시 확인 */ }
}

// 확신이 없을 때(문장이 비슷한 예시로 짐작했거나 못 알아들음) 무엇을 원했는지 고르는 버튼. 고르면 서버에 가르치고(이 컴퓨터에만 저장) 그 질문을 다시 처리한다.
function renderAgentTeach(teach) {
  const box = $('agentTeach');
  const choices = $('agentTeachChoices');
  choices.innerHTML = '';
  if (!teach) { box.style.display = 'none'; return; }
  const understood = teach.understood;
  $('agentTeachText').textContent = understood
    ? '문장이 비슷한 예시로 "' + understood.label + '"(으)로 이해해 답했어요(확신도 ' + Math.round(understood.score * 100) + '%). 아니면 원하시는 걸 골라 주세요.'
    : '무엇을 보여달라는 질문인지 몰랐어요. 골라 주시면 보여드리고 기억해 둘게요.';
  (teach.choices || []).forEach(choice => {
    const button = document.createElement('button');
    button.type = 'button';
    const same = understood && understood.intent === choice.intent;
    button.textContent = (same ? '✓ 맞아요: ' : '') + choice.label;
    button.addEventListener('click', async () => {
      hideEl($('agentError'));
      const { ok, data } = await postJson('/api/agent/teach', { question: teach.question, intent: choice.intent });
      if (!ok) return showMsg($('agentError'), data.error || '가르치지 못했습니다.');
      $('agentTeachCount').textContent = '저장됨 · 가르친 문장 ' + data.count + '개';
      if (!same) { $('agentQuestion').value = teach.question; runAgentQuestion(); }
    });
    choices.appendChild(button);
  });
  box.style.display = '';
}

// 답변의 표(tables): {title, columns, align, wrap, rows, footer}. 칸은 글자이거나 {links: [{text, href, meta}]}(뉴스 제목 링크).
// 칸의 내용에 따라 모양을 입힌다: 매수/매도는 뱃지, 진입 비율은 막대, '돌파 중'은 빨강, '과거 돌파'는 노랑, 값이 없으면 흐리게.
function agentCellText(c) {
  if (c && typeof c === 'object' && Array.isArray(c.lines)) return c.lines.join(' / ');
  if (c && typeof c === 'object' && Array.isArray(c.links)) {
    return c.links.map(l => l.text + (l.meta ? ' (' + l.meta + ')' : '')).join(' / ');
  }
  return String(c ?? '');
}

function agentCellHtml(column, cell) {
  if (cell && typeof cell === 'object' && Array.isArray(cell.lines)) {
    // 여러 줄 글 칸. 마지막 줄("그 밖 …")은 흐리게 보인다.
    return cell.lines.map((line, i) => '<div class="agent-line' + (i === cell.lines.length - 1 && cell.lines.length > 1 ? ' muted' : '') + '">' + esc(line) + '</div>').join('');
  }
  if (cell && typeof cell === 'object' && Array.isArray(cell.links)) {
    return cell.links.map(l => {
      const meta = l.meta ? '<span class="muted agent-news-meta"> · ' + esc(l.meta) + '</span>' : '';
      // 링크는 http(s)만 허용한다(서버도 걸러 주지만 화면에서 한 번 더 확인)
      return /^https?:\/\//i.test(String(l.href || ''))
        ? '<div class="agent-news-item"><a class="agent-link" href="' + esc(l.href) + '" target="_blank" rel="noopener noreferrer">' + esc(l.text) + '</a>' + meta + '</div>'
        : '<div class="agent-news-item">' + esc(l.text) + meta + '</div>';
    }).join('');
  }
  const t = String(cell ?? '');
  if (t === '') return '';
  if (t === '매수') return '<span class="agent-badge buy">매수</span>';
  if (t === '매도') return '<span class="agent-badge sell">매도</span>';
  if (column === '진입 비율' && /^[0-9.]+%$/.test(t)) {
    const pct = Math.max(0, Math.min(100, parseFloat(t) || 0));
    return '<span class="agent-meter"><span style="width:' + pct + '%"></span></span><span class="agent-meter-num">' + esc(t) + '</span>';
  }
  if (t === '-' || (/\(원\)$/.test(column) && (column.startsWith('지금') || column.startsWith('대기')) && t === '0')) {
    return '<span class="muted">' + (t === '-' ? '–' : '0') + '</span>';
  }
  return esc(t);
}

function agentCellClass(column, cell) {
  const t = agentCellText(cell);
  if (/돌파 중/.test(t)) return 'cell-down';
  if (/과거 돌파/.test(t)) return 'cell-warn';
  if (/^(데이터 부족|주수 확인 불가)/.test(t) || /신호 확인 불가|적용된 전략 없음|전략 제외|관련 보도를 찾지 못했습니다|뉴스를 불러오지 못했습니다|거의 설명되지 않음|데이터가 부족해/.test(t)) return 'cell-muted';
  if (column === '지금(원)' && t !== '0' && t !== '-') return 'cell-strong';
  if (column === '이번 주' && /^\+[0-9]/.test(t)) return 'cell-up';
  if (column === '이번 주' && /^-[0-9]/.test(t)) return 'cell-down';
  return '';
}

function renderAgentTables(tables) {
  const row = (cells, t, tag) => '<tr>' + cells.map((c, i) => {
    const classes = [];
    if (tag === 'td') {
      const cls = agentCellClass(t.columns[i], c);
      if (cls) classes.push(cls);
      if ((t.wrap || [])[i]) classes.push((t.wrap || []).filter(Boolean).length > 1 ? 'cell-wrap-multi' : 'cell-wrap'); // 줄바꿈 열이 여럿이면 열 폭을 줄여 한 화면에 담는다
    }
    const align = (t.align || [])[i] === 'r' ? 'right' : 'left';
    return '<' + tag + (classes.length ? ' class="' + classes.join(' ') + '"' : '') + ' style="text-align:' + align + '">' +
      (tag === 'th' ? esc(c) : agentCellHtml(t.columns[i], c)) + '</' + tag + '>';
  }).join('') + '</tr>';
  $('agentTables').innerHTML = (tables || []).map(t =>
    '<div class="agent-table-title">' + esc(t.title) + '</div><div class="agent-table-wrap' + (t.compact ? ' compact' : '') + '"><table class="agent-table">' +
    '<thead>' + row(t.columns, t, 'th') + '</thead>' +
    '<tbody>' + t.rows.map(r => row(r, t, 'td')).join('') + '</tbody>' +
    ((t.footer || []).length ? '<tfoot>' + t.footer.map(r => row(r, t, 'td')).join('') + '</tfoot>' : '') +
    '</table></div>'
  ).join('');
}

// 표를 말로 풀어 쓴 문자열(대화 기록에 넣어 다음 질문의 문맥으로 쓴다)
function agentTablesAsText(tables) {
  return (tables || []).map(t => t.title + '\n' + [t.columns].concat(t.rows, t.footer || []).map(r => r.map(agentCellText).join(' | ')).join('\n')).join('\n\n');
}

function renderAgentConversation() {
  $('agentConversation').innerHTML = agentHistory.map(item =>
    '<div class="agent-message ' + esc(item.role) + '">' + esc(item.content) + '</div>'
  ).join('');
}

const FRED_SOURCE_TEXT = { session: '이번 세션에 입력한 키', keychain: '키체인에 저장된 키(자동 연결)', env: '환경변수 FRED_API_KEY(자동 연결)' };
function setFredStatus(data) {
  $('fredStatus').textContent = data.fred_configured
    ? '현재: FRED 연결됨 · ' + (FRED_SOURCE_TEXT[data.fred_source] || '')
    : '현재: 연결 안 됨(야후 대용 시세 사용)';
  const supported = !!data.keychain_supported;
  $('fredSaveLabel').style.display = supported ? '' : 'none';      // 키체인은 macOS에서만
  $('fredForgetBtn').style.display = supported ? '' : 'none';
}
function setAgentMode(configured, model) {
  $('agentMode').textContent = configured
    ? 'AI 연결 · ' + model + ' · 읽기 전용'
    : '규칙 기반 대체 모드 · 읽기 전용';
}

async function loadAgentConfig() {
  try {
    const { ok, data } = await getJson('/api/agent/config');
    if (!ok) return;
    $('agentModel').value = data.model || 'claude-opus-5-5';
    setAgentMode(data.configured, data.model);
    setFredStatus(data);
    loadAgentMisses();
  } catch (e) { /* 다음 탭 진입이나 저장 때 다시 확인 */ }
}

async function saveAgentConfig(clearKey = false, clearFred = false, forgetFred = false) {
  hideEl($('agentConfigError'));
  hideEl($('agentConfigSaved'));
  const btn = forgetFred ? $('fredForgetBtn') : clearFred ? $('fredClearBtn') : clearKey ? $('agentConfigClearBtn') : $('agentConfigSaveBtn');
  btn.disabled = true;
  try {
    const { ok, data } = await postJson('/api/agent/config', {
      model: $('agentModel').value.trim(),
      api_key: clearKey ? '' : $('agentApiKey').value.trim(),
      clear_key: clearKey,
      fred_key: clearFred || forgetFred ? '' : $('fredApiKey').value.trim(),
      fred_save: $('fredSave').checked,
      clear_fred_key: clearFred,
      fred_forget: forgetFred,
    });
    if (!ok) return showMsg($('agentConfigError'), data.error || '설정을 저장하지 못했습니다.');
    $('agentApiKey').value = '';
    $('fredApiKey').value = '';
    $('fredSave').checked = false;
    setFredStatus(data);
    if (data.fred_save_error) showMsg($('agentConfigError'), data.fred_save_error);
    setAgentMode(data.configured, data.model);
    $('agentConfigSaved').style.display = 'inline';
  } catch (e) {
    showMsg($('agentConfigError'), '서버에 연결할 수 없습니다.');
  } finally {
    btn.disabled = false;
  }
}

async function runAgentQuestion() {
  const question = $('agentQuestion').value.trim();
  const btn = $('agentRunBtn');
  hideEl($('agentError'));
  if (!question) return showMsg($('agentError'), '질문을 입력해 주세요.');
  const historyForRequest = agentHistory.slice(-8).map(m => ({ role: m.role, content: (m.context || m.content).slice(0, 2000) })); // 표는 글로 풀어 문맥에만 포함(서버 한도 2,000자에 맞춤)
  agentHistory.push({ role: 'user', content: question });
  renderAgentConversation();
  $('agentQuestion').value = '';
  btn.disabled = true;
  btn.textContent = '계좌 확인 중...';
  try {
    const { ok, data } = await postJson('/api/agent/query', { question, history: historyForRequest });
    if (!ok) {
      agentHistory.pop();
      renderAgentConversation();
      if (data.error === '로그인이 필요합니다.') showLoggedOut();
      return showMsg($('agentError'), data.error || '질문을 처리하지 못했습니다.');
    }
    const executions = data.executions || [];
    $('agentTrace').innerHTML = executions.length
      ? executions.map((step, i) => '<div class="agent-step"><span>' + (i + 1) + '</span><div><b>' + esc(step.tool) + '</b><small>' + esc(step.reason ? '↳ ' + step.reason : (step.description || step.status || '완료')) + '</small></div></div>').join('')
      : '<div class="agent-empty">계좌 도구를 실행하지 않고 답했습니다.</div>';
    const isAi = data.mode === 'claude';
    const answerText = isAi ? data.answer_text : '';
    $('agentHeadline').textContent = isAi ? 'AI Agent 답변' : (data.answer.headline || '확인 결과');
    $('agentAnswerText').textContent = answerText;
    $('agentAnswerText').style.display = isAi ? '' : 'none';
    $('agentBullets').style.display = isAi ? 'none' : '';
    $('agentTables').style.display = isAi ? 'none' : '';
    $('agentCharts').style.display = isAi ? 'none' : '';
    $('agentNotes').style.display = isAi ? 'none' : '';
    $('agentLimitsWrap').style.display = !isAi && (data.answer.limitations || []).length ? '' : 'none';
    if (isAi) { renderAgentSuggestions([]); renderAgentTeach(null); renderAgentLearned(null); }
    if (!isAi) {
      renderAgentList('agentBullets', data.answer.bullets);
      renderAgentSuggestions(data.answer.suggestions);
      renderAgentTeach(data.answer.teach);
      renderAgentLearned(data.answer.learned);
      renderAgentCharts($('agentCharts'), data.answer.charts);
      renderAgentTables(data.answer.tables);
      renderAgentList('agentNotes', data.answer.notes);
      renderAgentList('agentLimits', data.answer.limitations);
    }
    // 말풍선에는 요약 문장만 보이고(표는 아래 결과 카드에 그려진다), 다음 질문의 문맥에는 표 내용도 글로 들어간다.
    agentHistory.push({
      role: 'assistant',
      content: isAi ? answerText : (data.answer.bullets || []).join('\n'),
      context: isAi ? undefined : [(data.answer.bullets || []).join('\n'), agentTablesAsText(data.answer.tables), (data.answer.notes || []).join('\n')].filter(Boolean).join('\n\n'),
    });
    renderAgentConversation();
    $('agentResult').style.display = 'block';
  } catch (e) {
    agentHistory.pop();
    renderAgentConversation();
    showMsg($('agentError'), '서버에 연결할 수 없습니다.');
  } finally {
    btn.disabled = false;
    btn.textContent = '확인';
  }
}

document.querySelectorAll('[data-agent-question]').forEach(button => {
  button.addEventListener('click', () => {
    $('agentQuestion').value = button.dataset.agentQuestion;
    runAgentQuestion();
  });
});
$('agentRunBtn').addEventListener('click', runAgentQuestion);
$('agentQuestion').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.isComposing && event.keyCode !== 229) runAgentQuestion();  // 한글 조합 중 Enter는 글자 확정일 뿐이라 전송하지 않는다(마지막 글자가 따로 남는 문제)
});
$('agentConfigSaveBtn').addEventListener('click', () => saveAgentConfig(false));
$('agentConfigClearBtn').addEventListener('click', () => saveAgentConfig(true));
$('fredClearBtn').addEventListener('click', () => saveAgentConfig(false, true));
$('fredForgetBtn').addEventListener('click', () => saveAgentConfig(false, false, true));
$('agentMissEnable').addEventListener('change', async () => {
  const { ok, data } = await postJson('/api/agent/misses', { enable: $('agentMissEnable').checked });
  if (ok) $('agentMissCount').textContent = (data.enabled ? '기록 켜짐' : '기록 꺼짐') + ' · ' + data.count + '건';
});
$('agentMissClearBtn').addEventListener('click', async () => {
  const { ok, data } = await postJson('/api/agent/misses', { clear: true });
  if (ok) { $('agentMissCount').textContent = '기록 ' + data.count + '건'; $('agentMissList').style.display = 'none'; }
});
$('agentMissViewBtn').addEventListener('click', async () => {
  const { ok, data } = await getJson('/api/agent/misses');
  if (!ok) return;
  const kinds = { unplaced: '못 알아들음', guessed: '짐작함' };
  const list = $('agentMissList');
  list.textContent = data.items.length
    ? data.items.map(r => '[' + (kinds[r.kind] || r.kind) + (r.guess ? ' → ' + r.guess : '') + '] ' + r.question + '  (' + r.at + ')').join('\n')
    : '기록이 없습니다.' + (data.enabled ? '' : ' (기록이 꺼져 있습니다)');
  list.style.display = '';
});
$('agentTeachClearBtn').addEventListener('click', async () => {
  const { ok, data } = await postJson('/api/agent/teach', { clear: true });
  $('agentTeachCount').textContent = ok ? '모두 지웠습니다' : (data.error || '지우지 못했습니다');
});
$('agentClearBtn').addEventListener('click', () => {
  agentHistory = [];
  renderAgentConversation();
  hideEl($('agentResult'));
});
