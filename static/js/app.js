// 로그인/로그아웃, 페이지 탭, 첫 로딩. 마지막에 로드된다(다른 화면 스크립트의 load* 함수를 부름).

function showLoggedIn() {
  $('loginCard').style.display = 'none';
  $('connectedView').style.display = 'block';
  $('logoutBtn').style.display = 'inline-block';
}

function showLoggedOut() {
  $('loginCard').style.display = 'block';
  $('connectedView').style.display = 'none';
  $('logoutBtn').style.display = 'none';
}

async function enterDashboard() {
  showLoggedIn();
  await Promise.all([loadHoldings(), loadRebalanceConfig(), loadWatchlist(), loadMaRules(), loadStrategy()]);
}

async function checkSession() {
  const { data } = await getJson('/api/session');
  if (data.logged_in) await enterDashboard();
  else showLoggedOut();
}

$('loginBtn').addEventListener('click', async () => {
  const btn = $('loginBtn');
  const error = $('loginError');
  hideEl(error);
  const app_key = $('appKey').value.trim();
  const app_secret = $('appSecret').value.trim();
  if (!app_key || !app_secret) return showMsg(error, '앱키와 시크릿을 모두 입력하세요.');
  btn.disabled = true;
  btn.textContent = '연결 중...';
  try {
    const { ok, data } = await postJson('/api/login', { app_key, app_secret });
    if (!ok) return showMsg(error, data.error || '로그인에 실패했습니다.');
    $('appKey').value = '';
    $('appSecret').value = '';
    await enterDashboard();
  } catch (e) {
    showMsg(error, '서버에 연결할 수 없습니다.');
  } finally {
    btn.disabled = false;
    btn.textContent = '로그인';
  }
});

$('appSecret').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('loginBtn').click();
});

$('logoutBtn').addEventListener('click', async () => {
  await fetch('/api/logout', { method: 'POST' });
  showLoggedOut();
});

// 페이지 탭 (포트폴리오 / 주간 리포트). 주간 리포트는 종목이 많아(Yahoo 50여 개) 탭을 처음 열 때만 불러온다.
let weeklyReportLoaded = false;
document.querySelectorAll('.page-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.page-tab').forEach(t => t.classList.toggle('active', t === tab));
    const isReport = tab.dataset.page === 'report';
    $('pagePortfolio').style.display = isReport ? 'none' : '';
    $('pageReport').style.display = isReport ? '' : 'none';
    $('connectedView').classList.toggle('wide', isReport);
    if (isReport && !weeklyReportLoaded) {
      weeklyReportLoaded = true;
      loadWeeklyReport(false);
      loadWeeklyReportConfig();
    }
  });
});

checkSession();
