"""S4 검증 — E-number자동완성·서비스요청카드·JSON내보내기·Discussion타겟팅."""
import sys
import httpx

B = "http://127.0.0.1:8800"
P, F = [], []


def ok(n, c, x=""):
    (P if c else F).append(n)
    print(("  ✅ " if c else "  ❌ ") + n + ((" — " + str(x)[:120]) if x else ""))


HC = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "consultant1", "password": "pw"}).json()["token"]}
HA = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "admin", "password": "admin"}).json()["token"]}

cid = httpx.post(f"{B}/cases", headers=HC,
                 json={"company_name": "S4Test", "org_id": "org_demo"}).json()["case_id"]
print(f"공용 케이스: {cid[:8]}...")

# ── S4-1: E-number 자동완성 ──────────────────────────────
print(f"\n=== S4-1 E-number 자동완성 ===")
r1 = httpx.get(f"{B}/meta/ingredient-suggest?q=E44", headers=HC).json()
ok("E44 prefix → 결과 있음", len(r1) > 0, len(r1))
ok("첫 결과에 e_number 포함", "e_number" in r1[0], r1[0])
ok("첫 결과에 canonical_name 포함", "canonical_name" in r1[0], r1[0])
ok("E441(Gelatin) 포함", any(x["e_number"] == "E441" for x in r1), [x["e_number"] for x in r1])
ok("default_status 포함", "default_status" in r1[0], r1[0])
ok("severity 포함", "severity" in r1[0], r1[0])

r2 = httpx.get(f"{B}/meta/ingredient-suggest?q=gelatin", headers=HC).json()
ok("이름 검색 'gelatin' → 결과 있음", len(r2) > 0, len(r2))
ok("이름 검색 결과에 E441 포함", any(x.get("e_number") == "E441" for x in r2), [x.get("e_number") for x in r2])

r3 = httpx.get(f"{B}/meta/ingredient-suggest?q=", headers=HC).json()
ok("빈 쿼리 → 빈 리스트", r3 == [], r3)

r4 = httpx.get(f"{B}/meta/ingredient-suggest?q=E120", headers=HC).json()
ok("E120(카민) 검색", any(x.get("e_number") == "E120" for x in r4), [x.get("e_number") for x in r4])

# ── S4-2: 서비스 요청 카드 (인보이스 API) ──────────────
print(f"\n=== S4-2 서비스 요청 카드 — 인보이스 API (case={cid[:8]}) ===")
# 인보이스 API 자체 확인 (Dashboard에서 호출하는 것과 동일)
invs = httpx.get(f"{B}/cases/{cid}/invoices", headers=HC).json()
ok("인보이스 목록 조회", isinstance(invs, list), type(invs).__name__)

# 인보이스 생성 후 상태 확인
inv = httpx.post(f"{B}/cases/{cid}/invoices", headers=HC,
                 json={"service_type": "pre_audit", "amount": 500000}).json()
ok("인보이스 생성", "invoice_no" in inv, inv)

invs2 = httpx.get(f"{B}/cases/{cid}/invoices", headers=HC).json()
ok("생성 후 목록 1건 이상", len(invs2) >= 1, len(invs2))
ok("status=unpaid", invs2[0].get("status") == "unpaid", invs2[0].get("status"))
ok("total=PPN 포함", invs2[0].get("total", 0) > 500000, invs2[0].get("total"))

# ── S4-3: 케이스 JSON 내보내기 ───────────────────────────
print(f"\n=== S4-3 케이스 JSON 내보내기 (case={cid[:8]}) ===")
# 데이터 추가
pid = httpx.post(f"{B}/cases/{cid}/products", headers=HC,
                 json={"name": "TestProduct", "category": "food"}).json()["product_id"]
httpx.post(f"{B}/cases/{cid}/materials", headers=HC,
           json={"name": "Sugar", "e_number": "E330", "mat_type": "raw"})

resp = httpx.get(f"{B}/cases/{cid}/export.json", headers=HC)
ok("200 OK", resp.status_code == 200, resp.status_code)
data = resp.json()
ok("export_version 필드", "export_version" in data, data.get("export_version"))
ok("case 블록", "case" in data and data["case"]["case_id"] == cid, data.get("case", {}).get("case_id"))
ok("products 리스트", isinstance(data.get("products"), list) and len(data["products"]) >= 1, len(data.get("products", [])))
ok("materials 리스트", isinstance(data.get("materials"), list), type(data.get("materials")).__name__)
ok("invoices 리스트", isinstance(data.get("invoices"), list) and len(data["invoices"]) >= 1, len(data.get("invoices", [])))
ok("onsite_checklist 리스트", isinstance(data.get("onsite_checklist"), list), type(data.get("onsite_checklist")).__name__)
ok("auditor_pool 리스트", isinstance(data.get("auditor_pool"), list), type(data.get("auditor_pool")).__name__)

# ── S4-4: Discussion 섹션 타겟팅 ───────────────────────
print(f"\n=== S4-4 Discussion 섹션 타겟팅 (case={cid[:8]}) ===")
# 타겟별 토론 추가
httpx.post(f"{B}/cases/{cid}/discussions", headers=HC,
           json={"text": "원재료 E441 서류 미흡", "target": "materials", "kind": "repair"})
httpx.post(f"{B}/cases/{cid}/discussions", headers=HC,
           json={"text": "현장심사 NC 3건 시정필요", "target": "audit", "kind": "add"})
httpx.post(f"{B}/cases/{cid}/discussions", headers=HC,
           json={"text": "위원장 서명 필요", "target": "fatwa", "kind": "comment"})

# target=materials 필터
dm = httpx.get(f"{B}/cases/{cid}/discussions?target=materials", headers=HC).json()
ok("target=materials 필터 1건", len(dm) == 1, len(dm))
ok("target=materials 텍스트 확인", "E441" in dm[0]["text"], dm[0].get("text"))

# target=audit 필터
da = httpx.get(f"{B}/cases/{cid}/discussions?target=audit", headers=HC).json()
ok("target=audit 필터 1건", len(da) == 1, len(da))
ok("kind=add 확인", da[0].get("kind") == "add", da[0].get("kind"))

# target=fatwa 필터
df = httpx.get(f"{B}/cases/{cid}/discussions?target=fatwa", headers=HC).json()
ok("target=fatwa 필터 1건", len(df) == 1, len(df))

# 전체 (필터 없음) → 3건
dall = httpx.get(f"{B}/cases/{cid}/discussions", headers=HC).json()
ok("전체 discussions ≥3", len(dall) >= 3, len(dall))

# 없는 target → 빈 리스트
dn = httpx.get(f"{B}/cases/{cid}/discussions?target=nonexistent", headers=HC).json()
ok("없는 target → 빈 리스트", isinstance(dn, list) and len(dn) == 0, dn)

# ── 결과 ──────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASS {len(P)}/{len(P)+len(F)}")
if F:
    print("  실패:", ", ".join(F))
sys.exit(0 if not F else 1)
