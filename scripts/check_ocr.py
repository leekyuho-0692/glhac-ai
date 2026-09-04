#!/usr/bin/env python3
"""배포 점검 — OCR 이 '진짜로' 도는지 확인한다.

패키지가 설치돼 있어도 모델을 못 내려받으면 추론에서 실패한다. 그걸 첫 업로드에서
발견하면 이미 늦다 — 업체가 서류를 냈는데 글자가 안 읽히는 상태로 접수된다.
그래서 배포 직후 이 스크립트를 한 번 돌려 **실제 이미지로 추론까지** 확인한다.

    <venv>/bin/python scripts/check_ocr.py            # 합성 이미지로 점검
    <venv>/bin/python scripts/check_ocr.py 파일.png   # 실제 서류로 점검

종료코드 0=정상, 1=실패. 배포 스크립트에서 그대로 쓸 수 있다.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import ai_local  # noqa: E402

LANGS = [x.strip() for x in
         (os.environ.get("GLHAC_OCR_LANGS") or "id,korean").split(",") if x.strip()]


def _sample():
    """점검용 이미지 — 실제 서류가 없어도 추론 경로를 끝까지 태운다."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (900, 260), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.text((40, 60), "CATATAN PEMBELIAN BAHAN", fill=(0, 0, 0))
    d.text((40, 130), "No  Nama Bahan  Jumlah  Tanggal", fill=(0, 0, 0))
    f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    im.save(f.name)
    return f.name


def main() -> int:
    st = ai_local.ocr_status()
    print("1) 설치·모델")
    print("   paddleocr      : %s" % ("설치됨" if st["installed"] else "없음 ✗"))
    print("   모델 캐시      : %d종 · %s" % (st["models_cached"], st["model_dir"]))
    if st["note"]:
        print("   비고           : %s" % st["note"])
    if not st["installed"]:
        print("\n✗ paddleocr 가 없습니다. requirements.txt 로 설치하십시오.")
        return 1

    path, made = (sys.argv[1], False) if len(sys.argv) > 1 else (_sample(), True)
    print("\n2) 실제 추론 (%s · 언어 %s)" % (os.path.basename(path), ",".join(LANGS)))
    t0 = time.time()
    r = ai_local.ocr_text_multi(path, LANGS)
    dt = time.time() - t0
    if made:
        os.unlink(path)

    if not r.get("ok"):
        print("   ✗ 실패: %s" % str(r.get("error"))[:200])
        print("\n✗ OCR 이 동작하지 않습니다. 모델 다운로드(네트워크)를 확인하십시오.")
        return 1
    text = " ".join((r.get("text") or "").split())
    print("   소요           : %.1f초" % dt)
    print("   사용 언어      : %s" % (r.get("langs_used") or []))
    print("   인식 글자      : %d자" % len(text))
    print("   본문 앞부분    : %s" % text[:70])
    if not text.strip():
        print("\n✗ 글자를 하나도 읽지 못했습니다.")
        return 1
    print("\n✓ OCR 정상 — 스캔·사진 서류를 접수할 수 있습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
