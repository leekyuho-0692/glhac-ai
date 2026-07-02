"""P-6 프론트 RBAC 검증 — 역할별 nav 게이팅 + glhacCan 버튼게이팅 + admin 시스템설정."""
import sys
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8800/ui/"
NAV_EXPECT = {
    'consultant': 13, 'applicant': 9, 'penyelia_halal': 7,
    'pendamping_pph': 6, 'auditor': 4, 'fatwa_liaison': 4, 'admin': 14,
}
LOGIN = {'consultant': 'consultant1', 'applicant': 'applicant1', 'penyelia_halal': 'penyelia1',
         'pendamping_pph': 'pendamping1', 'auditor': 'auditor1', 'fatwa_liaison': 'fatwa1', 'admin': 'admin'}
# glhacCan 기대값
CAN = {
    'consultant': {'doc_review': 1, 'invoice_create': 1, 'lph_assign': 1, 'cert_issue': 1, 'fatwa_decide': 1, 'pendamping_verify': 0},
    'applicant': {'doc_review': 0, 'invoice_create': 0, 'sihalal_verify': 0, 'sihalal_link': 1, 'invoice_pay': 1, 'sjph_edit': 1, 'case_create': 1},
    'penyelia_halal': {'material_edit': 1, 'sjph_edit': 1, 'case_create': 0, 'doc_review': 0, 'lph_assign': 0},
    'pendamping_pph': {'pendamping_verify': 1, 'material_edit': 0, 'doc_review': 0, 'invoice_create': 0},
    'auditor': {'finding_edit': 1, 'lph_assign': 0, 'doc_review': 0, 'cert_issue': 0},
    'fatwa_liaison': {'fatwa_decide': 1, 'cert_issue': 0, 'doc_review': 0},
    'admin': {'doc_review': 1, 'cert_issue': 1, 'lph_assign': 1, 'fatwa_decide': 1, 'case_create': 1},
}
P, F = [], []


def ok(n, c, x=""):
    (P if c else F).append(n)
    print(("  ✅ " if c else "  ❌ ") + n + ((" — " + str(x)[:60]) if x else ""))


with sync_playwright() as p:
    br = p.chromium.launch()
    pg = br.new_page(viewport={"width": 1440, "height": 1000})
    pg.goto(URL)
    pg.wait_for_timeout(1000)

    print("=== 1) 역할별 nav 게이팅 ===")
    for role, cnt in NAV_EXPECT.items():
        u = LOGIN[role]
        pw = 'admin' if u == 'admin' else 'pw'
        pg.evaluate(f"quickLogin('{u}','{pw}')")
        pg.wait_for_timeout(800)
        n = pg.eval_on_selector_all("#nav button", "els=>els.length")
        ok(f"{role:<15} nav {cnt}개", n == cnt, f"got {n}")

    print("=== 2) glhacCan 버튼게이팅 로직 ===")
    for role, acts in CAN.items():
        pg.evaluate(f"localStorage.setItem('glhac_role','{role}')")
        bad = []
        for a, exp in acts.items():
            got = 1 if pg.evaluate(f"glhacCan('{a}')") else 0
            if got != exp:
                bad.append(f"{a}:{got}!={exp}")
        ok(f"{role:<15} glhacCan", not bad, ",".join(bad))

    print("=== 3) admin 시스템 설정 접근제어 ===")
    pg.evaluate("quickLogin('admin','admin')")
    pg.wait_for_timeout(1200)
    pg.evaluate("go('system')")
    pg.wait_for_timeout(1200)
    sysx = pg.inner_text("#system")
    ok("admin 시스템설정 렌더", all(k in sysx for k in ["사용자 관리", "조직 관리", "전체 케이스", "전역 감사"]))
    pg.evaluate("quickLogin('consultant1','pw')")
    pg.wait_for_timeout(1000)
    red = pg.evaluate("(function(){go('system');return currentSection})()")
    ok("consultant system→dashboard 리다이렉트", red == "dashboard", "sec=" + str(red))
    br.close()

print(f"\n=== 결과 === PASS {len(P)} / FAIL {len(F)}")
if F:
    print("실패:", F)
sys.exit(1 if F else 0)
