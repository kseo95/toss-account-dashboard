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

// 페이지 탭 (포트폴리오 / 주간 리포트 / 세금). 주간 리포트·세금은 탭을 처음 열 때만 불러온다.
const PAGES = { portfolio: 'pagePortfolio', report: 'pageReport', tax: 'pageTax' };
const pageLoaded = {};
const PAGE_LOADERS = {
  report: () => { loadWeeklyReport(false); loadWeeklyReportConfig(); },
  tax: () => { loadTax(); loadTrades(); loadDividends(); },
};
document.querySelectorAll('.page-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const page = tab.dataset.page;
    document.querySelectorAll('.page-tab').forEach(t => t.classList.toggle('active', t === tab));
    Object.entries(PAGES).forEach(([key, id]) => { $(id).style.display = key === page ? '' : 'none'; });
    $('connectedView').classList.toggle('wide', page === 'report');
    if (PAGE_LOADERS[page] && !pageLoaded[page]) {
      pageLoaded[page] = true;
      PAGE_LOADERS[page]();
    }
  });
});

checkSession();
