// 공용 도우미: DOM 찾기, 이스케이프, 숫자 포맷, API 호출, 저장 버튼 흐름, 종목 검색 자동완성.
// 모든 화면 스크립트(portfolio/watch/strategy/report/app.js)보다 먼저 로드된다.

const $ = id => document.getElementById(id);

// API에서 온 문자열(종목명·사용자가 입력한 이름 등)을 innerHTML에 넣기 전에 항상 거친다.
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtWon(n) {
  return Math.round(n).toLocaleString('ko-KR') + '원';
}
function fmtUsd(n) {
  const sign = n < 0 ? '-' : '';
  return sign + '$' + Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtPct(n) {
  return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
}
function fmtPrice(price, currency) {
  return currency === 'USD' ? fmtUsd(price) : Math.round(price).toLocaleString('ko-KR');
}
function signClass(n) {
  return n > 0 ? 'profit' : (n < 0 ? 'loss' : '');
}
function nameWithSymbol(name, symbol) {
  return `${esc(name)} <span class="muted">(${esc(symbol)})</span>`;
}
function parseSymbolList(text) {
  return text.split(/[,\s]+/).map(s => s.trim().toUpperCase()).filter(Boolean);
}

function showMsg(el, text) {
  el.textContent = text;
  el.style.display = 'block';
}
function hideEl(el) {
  el.style.display = 'none';
}

async function getJson(url) {
  const res = await fetch(url);
  return { ok: res.ok, status: res.status, data: await res.json().catch(() => ({})) };
}
async function postJson(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return { ok: res.ok, status: res.status, data: await res.json().catch(() => ({})) };
}

// 설정 저장 버튼 공통 흐름: 버튼 잠금 → POST → 실패면 에러 표시 / 성공이면 "저장됨" + onSuccess(data).
async function runSave({ btn, errEl, savedEl, url, body, onSuccess, busyText }) {
  hideEl(errEl);
  if (savedEl) hideEl(savedEl);
  const label = btn.textContent;
  btn.disabled = true;
  if (busyText) btn.textContent = busyText;
  try {
    const { ok, data } = await postJson(url, body);
    if (!ok) {
      showMsg(errEl, data.error || '저장에 실패했습니다.');
      return;
    }
    if (savedEl) savedEl.style.display = 'inline';
    if (onSuccess) await onSuccess(data);
  } catch (e) {
    showMsg(errEl, '서버에 연결할 수 없습니다.');
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

// 종목 검색 자동완성(관심종목·MA 감시·전략 배정이 같이 씀). 토스 API엔 검색이 없어서 서버가 네이버
// 자동완성 후보를 토스로 검증해서 돌려준다. 입력이 멈추고 300ms 뒤에 조회하고, 늦게 온 옛 응답은 버린다.
// tagFor(result)로 후보 옆에 붙일 문구(예: "이미 등록됨")를 정할 수 있다.
function attachSymbolSearch({ input, suggestions, onPick, tagFor = () => '' }) {
  let seq = 0;
  let timer = null;
  const hide = () => { suggestions.style.display = 'none'; suggestions.innerHTML = ''; };
  const render = results => {
    suggestions.innerHTML = results.length ? '' : '<div class="empty">검색 결과가 없습니다.</div>';
    results.forEach(r => {
      const div = document.createElement('div');
      div.className = 'item';
      div.innerHTML = `<span>${esc(r.name)}</span><span class="sym">${esc(r.symbol)}${esc(tagFor(r))}</span>`;
      div.addEventListener('click', () => {
        hide();
        input.value = '';
        onPick(r);
      });
      suggestions.appendChild(div);
    });
    suggestions.style.display = 'block';
  };
  input.addEventListener('input', () => {
    const q = input.value.trim();
    clearTimeout(timer);
    if (!q) return hide();
    timer = setTimeout(async () => {
      const mySeq = ++seq;
      try {
        const { ok, data } = await getJson('/api/search?q=' + encodeURIComponent(q));
        if (ok && mySeq === seq) render(data.results || []);
      } catch (e) { /* 자동완성 실패는 조용히 무시(다음 입력에서 다시 시도) */ }
    }, 300);
  });
  document.addEventListener('click', e => {
    if (!suggestions.parentElement.contains(e.target)) hide();
  });
}
