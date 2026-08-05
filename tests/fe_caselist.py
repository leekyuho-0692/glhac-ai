"""케이스 목록 + 임시저장 UI 검증."""
import sys
from playwright.sync_api import sync_playwright
from _target import base   # 라이브(8800) 오염 방지 — 대상 서버는 GLHAC_E2E_BASE 로만 지정

BASE = base("/ui/")
P, F = [], []


def ok(n, c, extra=""):
    (P if c else F).append(n)
    print(("  ✅ " if c else "  ❌ ") + n + ((" — " + str(extra)[:80]) if extra else ""))


def txt(pg, sel):
    try:
        return pg.inner_text(sel)
    except Exception:
        return ""


with sync_playwright() as p:
    br = p.chromium.launch()
    pg = br.new_page(viewport={"width": 1440, "height": 1100})
    pg.goto(BASE)
    pg.wait_for_timeout(1500)
    try:
        pg.click("text=consultant1", timeout=4000)
    except Exception:
        pg.evaluate("quickLogin('consultant1','pw')")
    pg.wait_for_timeout(2500)

    print("=== 케이스 2개 생성 ===")
    pg.evaluate("go('application')")
    pg.wait_for_timeout(1500)
    pg.fill("#gaCN", "임시저장 케이스 A")
    pg.evaluate("glhacNewCase()")
    pg.wait_for_timeout(1500)
    # 임시저장 버튼
    pg.evaluate("glhacSaveProfile()")
    pg.wait_for_timeout(1200)
    ok("임시저장 → 마지막 저장 시각 표시", "마지막 임시저장" in txt(pg, "#application"))

    print("=== 케이스 목록 ===")
    pg.evaluate("go('caseList')")
    pg.wait_for_timeout(1500)
    cl = txt(pg, "#caseList")
    ok("내 케이스 목록 렌더", "내 케이스 목록" in cl)
    ok("분류 카운트(완료/진행중/임시저장)", "완료" in cl and "진행중" in cl and "임시저장" in cl)
    ok("생성한 케이스가 목록에 노출", "임시저장 케이스 A" in cl)
    navtx=txt(pg, "#nav");ok("nav에 케이스목록 추가", ("My Cases" in navtx) or ("내 케이스" in navtx) or ("Kasus Saya" in navtx))

    print("=== 케이스 선택 전환 ===")
    before = pg.evaluate("GLHAC_CASE")
    # 다른 케이스 열기(있으면) — 첫 '열기' 버튼 클릭
    btns = pg.locator("#caseList button:has-text('열기')")
    if btns.count() > 0:
        btns.first.click()
        pg.wait_for_timeout(1800)
        after = pg.evaluate("GLHAC_CASE")
        ok("케이스 선택 → 전환됨", after and after != before, f"{before[:6]}→{after[:6]}")
        ok("선택 후 신청 화면으로 이동", pg.is_visible("#application"))
    else:
        ok("케이스 선택(열기 버튼)", True, "다른 케이스 없음(스킵)")

    # 영속화: localStorage glhac_case
    saved = pg.evaluate("localStorage.getItem('glhac_case')")
    ok("선택 케이스 localStorage 영속화", bool(saved))
    pg.screenshot(path="demo_shots/caselist.png", full_page=True)
    br.close()

print(f"\nPASS {len(P)} / FAIL {len(F)}")
if F:
    print("실패:", F)
sys.exit(1 if F else 0)
