"""633 도메인(화장품/의약품 패키징) 성분 용어로 샘플 라벨 이미지 생성.
원천이미지(150GB) 미보유 → 실제 성분 텍스트로 OCR 파이프라인 검증용."""
import os
from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "sample_label.png")
FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"

LINES = [
    "전성분 (INGREDIENTS)",
    "정제수, 글리세린(Glycerin), 젤라틴(Gelatin),",
    "부틸렌글라이콜, 레시틴(Lecithin),",
    "구연산(Citric Acid), 향료(Fragrance)",
    "Water, Glycerin, Gelatin, Lecithin, Citric Acid",
    "용량: 100ml / 제조: ABC Cosmetics",
]


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img = Image.new("RGB", (980, 460), "white")
    d = ImageDraw.Draw(img)
    title = ImageFont.truetype(FONT, 40)
    body = ImageFont.truetype(FONT, 34)
    d.text((40, 30), LINES[0], fill="black", font=title)
    y = 110
    for ln in LINES[1:]:
        d.text((40, y), ln, fill="black", font=body)
        y += 58
    img.save(OUT)
    print("saved:", OUT)


if __name__ == "__main__":
    main()