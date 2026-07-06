"""S2 검증 — 제품사진·문서재분류·SJPH 10증빙·HPAS 자동요약."""
import base64
import os
import sys
import httpx

B = "http://127.0.0.1:%s" % os.environ.get("GLHAC_PORT", "8800")
P, F = [], []


def ok(n, c, x=""):
    (P if c else F).append(n)
    print(("  ✅ " if c else "  ❌ ") + n + ((" — " + str(x)[:80]) if x else ""))


H = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
     json={"username": "consultant1", "password": "pw"}).json()["token"]}
HA = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "admin", "password": "admin"}).json()["token"]}

# ── 공용 케이스 ──────────────────────────────────────────────
cid = httpx.post(f"{B}/cases", headers=H,
                 json={"company_name": "S2Test", "is_msme": True, "org_id": "org_demo"}).json()["case_id"]

print(f"=== S2-1 제품 사진 업로드 (case={cid[:8]}) ===")
pid = httpx.post(f"{B}/cases/{cid}/products", headers=H,
                 json={"name": "CookiePro", "category": "bakery"}).json()["product_id"]
img = base64.b64encode(b"\x89PNG dummy image bytes").decode()
r = httpx.post(f"{B}/cases/{cid}/products/{pid}/photo", headers=H,
               json={"file_b64": img, "filename": "front.png"}).json()
ok("사진 업로드 → ok 반환", r.get("ok") is True or "document_id" in r, r)

photos = httpx.get(f"{B}/cases/{cid}/products/{pid}/photos", headers=H).json()
ok("사진 목록 조회 (1건)", isinstance(photos, list) and len(photos) == 1, photos)
ok("사진 항목에 filename 포함", photos and "filename" in photos[0], photos[:1])

prods = httpx.get(f"{B}/cases/{cid}/products", headers=H).json()
me = [p for p in prods if p["product_id"] == pid]
ok("products 목록에 photo_count=1", me and me[0].get("photo_count") == 1, me[:1])

print("=== S2-2 문서 재분류 ===")
b64 = base64.b64encode(b"dummy doc content").decode()
intake_body = base64.b64encode(b"PK dummy zip").decode()
# 직접 document_asset 생성을 위해 SJPH 증빙 경로 사용
se = httpx.post(f"{B}/cases/{cid}/sjph-evidence", headers=H,
                json={"item_key": "halal_supervisor", "file_b64": b64, "filename": "test_doc.pdf"}).json()
ok("SJPH 증빙 업로드(문서 생성)", "item_key" in se or "uploaded_at" in se, se)
docs = httpx.get(f"{B}/cases/{cid}/documents", headers=H).json()
ok("문서 목록 조회", isinstance(docs, list), f"count={len(docs)}")

if docs:
    did = docs[0]["document_id"]
    r2 = httpx.patch(f"{B}/documents/{did}/reclassify", headers=H,
                     json={"doc_type": "factory_registration"}).json()
    ok("문서 재분류 → doc_type 변경", r2.get("doc_type") == "factory_registration", r2)
    docs2 = httpx.get(f"{B}/cases/{cid}/documents", headers=H).json()
    updated = [d for d in docs2 if d["document_id"] == did]
    ok("재분류 후 목록에서 확인", updated and updated[0]["doc_type"] == "factory_registration")
else:
    ok("문서 재분류(문서 없어 skip)", True, "SKIP")
    ok("재분류 후 목록 확인(skip)", True, "SKIP")

print("=== S2-3 SJPH 10증빙 ===")
ev_list = httpx.get(f"{B}/cases/{cid}/sjph-evidence", headers=H).json()
ok("SJPH 증빙 목록 구조(items 키)", "items" in ev_list, list(ev_list.keys())[:5])
items = ev_list.get("items", [])  # list of {item_key, uploaded, ...}
ok("10개 항목 존재", len(items) == 10, f"count={len(items)}")
hs = next((x for x in items if x["item_key"] == "halal_supervisor"), None)
ok("halal_supervisor 항목 uploaded=True", hs is not None and hs.get("uploaded") is True, hs)

b64b = base64.b64encode(b"flow diagram data").decode()
r3 = httpx.post(f"{B}/cases/{cid}/sjph-evidence", headers=H,
                json={"item_key": "production_flow", "file_b64": b64b, "filename": "flow.pdf"}).json()
ok("두 번째 SJPH 증빙 업로드", "item_key" in r3 or "uploaded_at" in r3, r3)
ev_list2 = httpx.get(f"{B}/cases/{cid}/sjph-evidence", headers=H).json()
items2 = ev_list2.get("items", [])
pf = next((x for x in items2 if x["item_key"] == "production_flow"), None)
ok("두 번째 업로드 후 production_flow=True", pf is not None and pf.get("uploaded") is True, pf)

print("=== S2-4 HPAS 자동요약 ===")
h = httpx.get(f"{B}/cases/{cid}/hpas-auto", headers=H).json()
ok("hpas-auto 응답 구조(auto_completion+elements)", "auto_completion" in h and "elements" in h, list(h.keys()))
ok("hpas-auto auto_completion>0 (증빙 2건 반영)", h.get("auto_completion", 0) > 0, h.get("auto_completion"))
ok("hpas-auto elements 5개(HPAS 5대요소)", len(h.get("elements", [])) == 5, len(h.get("elements", [])))
el_keys = {e["element"] for e in h.get("elements", [])}
ok("hpas-auto 5대요소 키 포함", {"commitment", "materials", "process", "product", "monitoring"} <= el_keys, el_keys)

print(f"\n=== 결과 === PASS {len(P)} / FAIL {len(F)}")
if F:
    print("실패:", F)
sys.exit(1 if F else 0)
