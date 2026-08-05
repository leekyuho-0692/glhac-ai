"""프론트엔드 E2E — 실제 UI 이벤트(핸들러) → 엔드포인트 → 리턴 → DOM 반영 전수 검증 (Playwright)."""
import os
import sys
from playwright.sync_api import sync_playwright
from _target import base   # 라이브(8800) 오염 방지 — 대상 서버는 GLHAC_E2E_BASE 로만 지정

BASE = base("/ui/")
OUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "demo_shots")
os.makedirs(OUT, exist_ok=True)
P, F = [], []


def ok(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ((" — " + str(extra)[:80]) if extra else ""))


def txt(pg, sel):
    try:
        return pg.inner_text(sel)
    except Exception:
        return ""


def main():
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1440, "height": 1100})
        pg.goto(BASE)
        pg.wait_for_timeout(1500)

        print("=== 로그인 ===")
        try:
            pg.click("text=consultant1", timeout=4000)
        except Exception:
            pg.evaluate("quickLogin('consultant1','pw')")
        pg.wait_for_timeout(2500)
        ok("로그인 → 앱셸 표시", pg.is_visible("#appShell"))
        ok("대시보드 통계 렌더(백엔드)", "총 케이스" in txt(pg, "#dashboard"))

        print("=== 새 케이스 (frontend→/cases) ===")
        pg.evaluate("go('application')")
        pg.wait_for_timeout(1800)
        pg.fill("#gaCN", "Demo Co FE")
        pg.evaluate("glhacNewCase()")
        pg.wait_for_timeout(1800)
        ok("새 케이스 생성 → 백엔드 케이스 표시", "Demo Co FE" in txt(pg, "#application"))

        print("=== Materials (이벤트→/materials→표) ===")
        pg.evaluate("go('materials')")
        pg.wait_for_timeout(1500)
        pg.fill("#mfName", "gelatin")
        pg.evaluate("glhacAddMatFull()")
        pg.wait_for_timeout(1600)
        mt = txt(pg, "#materials")
        ok("원재료 추가 → 표 반영 + 판정", "gelatin" in mt.lower() and "NEEDS_EVIDENCE" in mt)
        pg.fill("#gmText", "gelatin, ethanol, garam")
        pg.evaluate("glhacScanText()")
        pg.wait_for_timeout(2200)
        ok("텍스트 스캐너 → 판정 표시", "위험" in txt(pg, "#gmTextOut"))

        print("=== Application (Penyelia·SIHALAL·경로판정) ===")
        pg.evaluate("go('application')")
        pg.wait_for_timeout(1600)
        pg.fill("#gaPN", "Budi FE")
        pg.evaluate("glhacAddPenyelia()")
        pg.wait_for_timeout(1500)
        ok("Penyelia 추가 → 활성 표시", "활성" in txt(pg, "#application"))
        pg.fill("#gaSE", "fe@x.com")
        pg.evaluate("glhacSihalalLink()")
        pg.wait_for_timeout(1300)
        pg.fill("#gaSV", "fe@x.com")
        pg.evaluate("glhacSihalalVerify()")
        pg.wait_for_timeout(1600)
        ok("SIHALAL 동일성 검증 → 연동됨", "연동됨" in txt(pg, "#application"))
        pg.evaluate("glhacRunAssessment()")
        pg.wait_for_timeout(2200)
        ok("AI 사전평가 → 경로판정 단계(AI 판정 노출)", "AI 판정" in txt(pg, "#application"))

        print("=== Invoice (PPN·게이트) ===")
        pg.evaluate("go('invoice')")
        pg.wait_for_timeout(1500)
        pg.fill("#ivAmt", "2000000")
        pg.evaluate("glhacAddInvoice()")
        pg.wait_for_timeout(1500)
        iv = txt(pg, "#invoice")
        ok("청구 생성 → INV·PPN 표시", "INV-" in iv and "220,000" in iv)

        print("=== Audit (지적) ===")
        pg.evaluate("go('audit')")
        pg.wait_for_timeout(1500)
        pg.fill("#afFind", "교차오염 위험 FE")
        pg.evaluate("glhacAddFinding()")
        pg.wait_for_timeout(1500)
        ok("심사지적 추가 → 표 반영", "교차오염 위험 FE" in txt(pg, "#audit"))

        print("=== SJPH (5요소) ===")
        pg.evaluate("go('sjphEvaluation')")
        pg.wait_for_timeout(1500)
        pg.evaluate("glhacSjphSet('commitment','ok')")
        pg.wait_for_timeout(1500)
        ok("SJPH 요소 ok → 완료율 표시", "완료율" in txt(pg, "#sjphEvaluation"))

        print("=== Fatwa (승인·freeze) ===")
        pg.evaluate("go('fatwa')")
        pg.wait_for_timeout(1500)
        pg.evaluate("glhacFatwa('approved')")
        pg.wait_for_timeout(1500)
        fw = txt(pg, "#fatwa")
        ok("Fatwa 승인 → approved + scope 동결", "approved" in fw and "동결" in fw)

        print("=== Certificate (발급·변경영향) ===")
        pg.evaluate("go('certificate')")
        pg.wait_for_timeout(1500)
        pg.evaluate("glhacIssueCert()")
        pg.wait_for_timeout(1800)
        ok("인증서 발급 → HC 표시", "HC-" in txt(pg, "#certificate"))
        pg.evaluate("glhacChangeImpact()")
        pg.wait_for_timeout(1500)
        ok("변경영향 분석 → 영향도 표시", "영향도" in txt(pg, "#certificate"))

        print("=== Documents (체크리스트) ===")
        pg.evaluate("go('documents')")
        pg.wait_for_timeout(1500)
        ok("문서 체크리스트 렌더", "필수서류 체크리스트" in txt(pg, "#documents"))

        print("=== Assistant (gemma3) ===")
        pg.evaluate("go('assistant')")
        pg.wait_for_timeout(1200)
        pg.fill("#aiQ", "지금 상태와 다음 할 일 알려줘")
        pg.evaluate("glhacAsk()")
        pg.wait_for_timeout(16000)
        ok("Copilot 응답 → 답변 렌더", "답변" in txt(pg, "#assistant"))

        print("=== Discussion ===")
        pg.fill("#dsText", "데모 토론 FE")
        pg.evaluate("glhacAddDisc()")
        pg.wait_for_timeout(1500)
        ok("토론 등록 → 패널 반영", "데모 토론 FE" in txt(pg, "#discussionPanel"))

        print("=== Dashboard (감사로그) ===")
        pg.evaluate("go('dashboard')")
        pg.wait_for_timeout(2000)
        ds = txt(pg, "#dashboard")
        ok("대시보드 감사 해시체인 무결성", "무결성 OK" in ds)
        pg.screenshot(path=f"{OUT}/fe_final.png", full_page=True)
        br.close()

    print(f"\n=== 결과 ===\nPASS {len(P)} / FAIL {len(F)}")
    if F:
        print("실패:", F)
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
