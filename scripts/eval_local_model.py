"""로컬 모델(Ollama)의 도구 선택 정확도 시험(실제 모델 호출 — Ollama가 켜져 있고 모델이 받아져 있어야 한다). 프롬프트나 모델을 바꾼 뒤 다시 돌려 비교한다.
사용: `python3 scripts/eval_local_model.py [모델 이름]`  (자동 시험이 아니다: 모델 호출은 느리고 결과가 매번 조금 다를 수 있다)"""
import sys, time, json, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5:7b-instruct"
import local_model as lm
# (질문, 기대 도구 집합(정답 후보 중 하나라도 포함하면 맞음), 설명)
CASES = [
 ("엔비디아 PER 어때?", [{"get_valuation"}]),
 ("TSLA 고점 대비 왜 빠졌어?", [{"get_drawdown_analysis"}]),
 ("삼성전자 RSI 과매수야?", [{"get_indicators"}]),
 ("미국 금리가 오르면 금과 나스닥은 어떻게 돼?", [{"get_factor_response"}]),
 ("이번 주 뭘 확인해야 해?", [{"get_portfolio_overview"},{"get_weekly_risk_report"},{"get_portfolio_overview","get_weekly_risk_report"}]),
 ("지금 미국 업종별 ETF 상태 어때?", [{"get_sector_trends"}]),
 ("전부 팔면 세금이 얼마야?", [{"estimate_liquidation_tax"}]),
 ("애플 뉴스랑 전망 알려줘", [{"get_weekly_news","get_outlook"},{"get_weekly_news"},{"get_outlook"}]),
 ("내 종목 중에 저평가인 거 있어?", [{"get_valuation"}]),
 ("팔란티어가 요즘 왜 이렇게 올랐어", [{"get_macro_drivers"},{"get_weekly_news"},{"get_macro_drivers","get_weekly_news"},{"get_etf_breakdown"}]),
 ("달러가 내리면 금은 어떻게 돼?", [{"get_factor_response"}]),
 ("S&P500 중에 주봉 200선 아래에 있는 종목 알려줘", [{"get_ma_screen"}]),
 ("금이 평소에 제일 영향받는 요인이 뭐야?", [{"get_asset_drivers"}]),
 ("2008년 금융위기 때 금은 어땠어?", [{"get_macro_cases"}]),
 ("내 포트폴리오 섹터 비중 보여줘", [{"get_sector_exposure"}]),
 ("리밸런싱 얼마씩 해야 돼?", [{"get_portfolio_overview"}]),
 ("테슬라 볼린저밴드 어때", [{"get_indicators"}]),
 ("하이닉스 앞으로 어떻게 될 것 같아?", [{"get_outlook"}]),
 ("UFO ETF 안에 뭐가 들어있어?", [{"get_etf_breakdown"}]),
 ("꼭대기에서 얼마나 내려왔고 왜 그런지 알려줘 (엔비디아)", [{"get_drawdown_analysis"}]),
 ("삼성전자 배당 얼마나 늘었어?", [set()]),          # 기능 없음 → 도구를 부르면 안 된다
 ("테슬라 100주 지금 사줘", [set()]),                  # 주문 → 도구 호출 금지
 ("오늘 점심 뭐 먹지", [set()]),                       # 무관
 ("엔비디아 목표주가 얼마야", [set(), {"get_outlook"}, {"get_valuation"}]),   # 기능 없음(가까운 도구를 불러도 규칙 쪽 "※ 목표주가 데이터는 없습니다" 메모가 붙는다)
 ("지금 시장 분위기가 어때", [{"get_sector_trends"},{"get_macro_drivers"},{"get_weekly_risk_report"}]),
]
ok=0; rows=[]
t0=time.time()
for q,gold in CASES:
    t=time.time()
    try:
        got=lm.plan(q, [], MODEL)
        names={c["name"] for c in got["calls"]}
        good = any(names==g for g in gold) or (any(g and g<=names for g in gold) and len(names)<=3 and not any(not g for g in gold))
        rows.append((good,q,sorted(names),got["reply"][:20],round(time.time()-t,1)))
        ok+=good
    except Exception as e:
        rows.append((False,q,"ERR "+type(e).__name__,str(e)[:60],round(time.time()-t,1)))
for good,q,names,reply,sec in rows:
    print("O" if good else "X", f"{sec:>5}s", q, "->", names, ("| "+reply) if reply and not names else "")
print(f"정확도 {ok}/{len(CASES)} · 총 {time.time()-t0:.0f}초")
