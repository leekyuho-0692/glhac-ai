"""633 라벨 OCR 평가 하베스 (설계 C.5).
원천이미지(150GB) 미보유 → 라벨의 정답 텍스트를 렌더→PaddleOCR→정답 대조하는 프록시 평가.
지표: CER(문자오류율), 라인 recall(정답 라인이 OCR 결과에 포함된 비율)."""
import os
import sys
import glob
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from app import ai_local  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(__file__))
LBL = os.path.join(ROOT, "data", "aihub", "633", "_vl1")
FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
TMP = "/tmp/glhac_eval"
os.makedirs(TMP, exist_ok=True)


def lev(a, b):
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def render(lines, path):
    fnt = ImageFont.truetype(FONT, 34)
    img = Image.new("RGB", (1000, max(120, 40 + len(lines) * 52)), "white")
    d = ImageDraw.Draw(img)
    y = 20
    for ln in lines:
        d.text((30, y), ln, fill="black", font=fnt)
        y += 52
    img.save(path)


def main(n=12, max_lines=10):
    files = sorted(glob.glob(os.path.join(LBL, "**", "annotations", "*.json"), recursive=True))[:n]
    if not files:
        print("라벨 파일 없음:", LBL)
        return
    print(f"[샘플 {len(files)}건] 라벨 정답 렌더 → PaddleOCR → 대조 (프록시 평가)")
    tot_cer = tot_recall = 0.0
    cnt = 0
    for f in files:
        d = json.load(open(f))
        gts = []
        for a in d.get("annotations", []):
            for p in a.get("polygons", []):
                t = (p.get("text") or "").strip()
                if t:
                    gts.append(t)
        gts = gts[:max_lines]
        if not gts:
            continue
        img = os.path.join(TMP, "e.png")
        render(gts, img)
        ocr = [ln["text"] for ln in ai_local.ocr_image(img).get("lines", [])]
        gt_join = "".join(g.replace(" ", "") for g in gts)
        ocr_join = "".join(t.replace(" ", "") for t in ocr)
        cer = lev(gt_join, ocr_join) / max(1, len(gt_join))
        found = sum(1 for g in gts if g.replace(" ", "") in ocr_join)
        recall = found / len(gts)
        tot_cer += cer
        tot_recall += recall
        cnt += 1
        print(f"  {os.path.basename(f):<26} CER={cer:.3f}  라인recall={recall:.2f} ({found}/{len(gts)})")
    if cnt:
        print(f"\n평균 CER={tot_cer/cnt:.3f} (문자정확도≈{1-tot_cer/cnt:.1%})  "
              f"평균 라인recall={tot_recall/cnt:.1%}  [n={cnt}]")
        print("주의: 라벨 렌더 프록시 평가. 실사진 정확도는 633 원천이미지(150GB) 확보 후 측정.")


if __name__ == "__main__":
    main()