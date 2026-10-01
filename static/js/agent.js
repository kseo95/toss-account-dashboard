// OpenAI 도구 호출 Agent. 대화 기록은 브라우저 메모리에만 두고 서버에는 최근 문맥만 보낸다.
let agentHistory = [];

function renderAgentList(id, items) {
  $(id).innerHTML = (items || []).map(item => '<li>' + esc(item) + '</li>').join('');
}

function renderAgentConversation() {
  $('agentConversation').innerHTML = agentHistory.map(item =>
    '<div class="agent-message ' + esc(item.role) + '">' + esc(item.content) + '</div>'
  ).join('');
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
    $('agentModel').value = data.model || 'gpt-6-luna';
    setAgentMode(data.configured, data.model);
  } catch (e) { /* 다음 탭 진입이나 저장 때 다시 확인 */ }
}

async function saveAgentConfig(clearKey = false) {
  hideEl($('agentConfigError'));
  hideEl($('agentConfigSaved'));
  const btn = clearKey ? $('agentConfigClearBtn') : $('agentConfigSaveBtn');
  btn.disabled = true;
  try {
    const { ok, data } = await postJson('/api/agent/config', {
      model: $('agentModel').value.trim(),
      api_key: clearKey ? '' : $('agentApiKey').value.trim(),
      clear_key: clearKey,
    });
    if (!ok) return showMsg($('agentConfigError'), data.error || '설정을 저장하지 못했습니다.');
    $('agentApiKey').value = '';
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
  const historyForRequest = agentHistory.slice(-8);
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
      ? executions.map((step, i) => '<div class="agent-step"><span>' + (i + 1) + '</span><div><b>' + esc(step.tool) + '</b><small>' + esc(step.description || step.status || '완료') + '</small></div></div>').join('')
      : '<div class="agent-empty">계좌 도구를 실행하지 않고 답했습니다.</div>';
    const isAi = data.mode === 'openai';
    const answerText = isAi ? data.answer_text : '';
    $('agentHeadline').textContent = isAi ? 'AI Agent 답변' : (data.answer.headline || '확인 결과');
    $('agentAnswerText').textContent = answerText;
    $('agentAnswerText').style.display = isAi ? '' : 'none';
    $('agentBullets').style.display = isAi ? 'none' : '';
    $('agentLimitsWrap').style.display = !isAi && (data.answer.limitations || []).length ? '' : 'none';
    if (!isAi) {
      renderAgentList('agentBullets', data.answer.bullets);
      renderAgentList('agentLimits', data.answer.limitations);
    }
    agentHistory.push({ role: 'assistant', content: isAi ? answerText : (data.answer.bullets || []).join('\n') });
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
  if (event.key === 'Enter') runAgentQuestion();
});
$('agentConfigSaveBtn').addEventListener('click', () => saveAgentConfig(false));
$('agentConfigClearBtn').addEventListener('click', () => saveAgentConfig(true));
$('agentClearBtn').addEventListener('click', () => {
  agentHistory = [];
  renderAgentConversation();
  hideEl($('agentResult'));
});
