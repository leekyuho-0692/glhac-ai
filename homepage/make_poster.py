# -*- coding: utf-8 -*-
"""2번 슬라이드 시안의 영문·인니어판 — 한국어 글자만 갈아 끼운다.

## 왜 다시 그리지 않고 '지우고 얹나'

오른쪽 사진(악수·모스크·항만)과 GLHAC 로고, 아이콘은 언어와 무관하다. 새로 만들면
같은 그림이 안 나온다. 그래서 **한국어가 있는 왼쪽 패널만** 배경으로 덮고 다시 쓴다.

배경은 통짜 흰색이 아니라 줄마다 미세하게 다르고, 오른쪽으로 갈수록 사진이 번져
들어온다. 그래서 각 줄의 왼쪽 끝(x=14~26) 실제 색을 떠서 칠하고, 오른쪽 경계는
원본과 서서히 섞는다.

덮는 폭은 줄마다 다르다 — 본문은 x≈600 까지, 아이콘 라벨은 x≈770 까지 한국어가
뻗어 있다(실측). 한 폭으로 덮으면 '합니다.' 같은 꼬리가 남는다.

로고와 아이콘은 덮기 전에 떠 두었다가 가장자리를 흐리게 해서 도로 붙인다 —
그냥 붙이면 네모 이음매가 보인다.

문구는 사이트 번역을 그대로 쓴다(aboutTitle·aboutP1·aboutP2·check1~4).
포스터에만 있는 두 줄은 여기서 옮겼다.
"""
import io
import json

from PIL import Image, ImageDraw, ImageFont

SRC = "/Users/dany/Downloads/image.png"
OUT = "/Users/dany/Downloads/GLHAC_홈페이지_인트로적용"

I18N = json.load(io.open("/tmp/about_i18n.json", encoding="utf-8"))
IDX = {"en": 1, "id": 2}


def t(k, lang):
    return I18N[k][IDX[lang]].replace("<br>", "\n")


TXT = {
    "en": dict(sub="A dedicated space for corporate halal certification consulting",
               eye="TRUSTED PARTNER",
               head="Right from the start,\ntogether to the end",
               about=t("aboutEyebrow", "en"), lead=t("aboutTitle", "en"),
               p1=t("aboutP1", "en"), p2=t("aboutP2", "en"),
               checks=[t("check%d" % i, "en") for i in range(1, 5)],
               tag="HALAL TODAY, A BRIGHTER TOMORROW"),
    "id": dict(sub="Ruang konsultasi sertifikasi halal untuk perusahaan",
               eye="MITRA TEPERCAYA",
               head="Benar sejak awal,\nbersama hingga akhir",
               about=t("aboutEyebrow", "id"), lead=t("aboutTitle", "id"),
               p1=t("aboutP1", "id"), p2=t("aboutP2", "id"),
               checks=[t("check%d" % i, "id") for i in range(1, 5)],
               tag="HALAL HARI INI, MASA DEPAN YANG LEBIH CERAH"),
}

# (y시작, y끝, 덮을 폭, 섞을 구간) — 한국어가 뻗은 만큼만 덮는다.
# 로고(y<130)와 아이콘(y 790~860)은 아예 건드리지 않는다. 떴다가 도로 붙이면
# 아무리 흐리게 해도 네모 자국이 남았다 — 손대지 않는 게 가장 깨끗하다.
ZONES = [(132, 610, 560, 150),
         (610, 788, 625, 150),        # 본문 두 문단
         (864, 940, 785, 115),        # 아이콘 라벨(아이콘 자체는 제외)
         (940, 1024, 600, 150)]
ICONS = [121, 281, 442, 632]

GREEN_D, GOLD, INK, GREY, GREEN = (28, 74, 60), (150, 112, 45), (42, 54, 50), (92, 104, 100), (20, 92, 74)
FDIR = "/System/Library/Fonts/Supplemental/%s"
def font(n, s): return ImageFont.truetype(FDIR % n, s)


def build(lang):
    im = Image.open(SRC).convert("RGB")
    W, H = im.size
    px = im.load()

    for y0, y1, end, feat in ZONES:
        for y in range(y0, min(y1, H)):
            base = tuple(sum(px[x, y][c] for x in range(14, 26)) // 12 for c in range(3))
            for x in range(end):
                px[x, y] = base
            for i in range(feat):
                x = end + i
                if x >= W:
                    break
                k = i / feat
                o = px[x, y]
                px[x, y] = tuple(int(base[c] * (1 - k) + o[c] * k) for c in range(3))

    d = ImageDraw.Draw(im)
    T = TXT[lang]

    def wrap(text, fnt, width, maxlines=99):
        out = []
        for para in text.split("\n"):
            line = ""
            for w in para.split():
                trial = (line + " " + w).strip()
                if d.textlength(trial, font=fnt) <= width:
                    line = trial
                else:
                    out.append(line); line = w
            out.append(line)
        return out[:maxlines]

    def block(lines, fnt, x, y, fill, lead):
        for ln in lines:
            d.text((x, y), ln, font=fnt, fill=fill); y += lead
        return y

    d.line([(40, 160), (68, 160)], fill=GREEN_D, width=3)
    block(wrap(T["sub"], font("Avenir Next.ttc", 20), 460), font("Avenir Next.ttc", 20),
          80, 148, GREEN_D, 26)

    d.text((80, 230), " ".join(T["eye"]), font=font("Avenir Next.ttc", 16), fill=GREY)

    f_head = font("Avenir Next.ttc", 54)
    y = 278
    for i, ln in enumerate(T["head"].split("\n")):
        d.text((76, y), ln, font=f_head, fill=GREEN_D if i == 0 else GOLD); y += 72

    d.text((80, 484), T["about"], font=font("Georgia Bold.ttf", 29), fill=GREEN_D)
    aw = d.textlength(T["about"], font=font("Georgia Bold.ttf", 29))
    d.line([(96 + aw, 500), (150 + aw, 500)], fill=GOLD, width=2)

    f_lead = font("Avenir Next.ttc", 24)
    block(wrap(T["lead"], f_lead, 490), f_lead, 82, 538, INK, 32)

    f_p = font("Avenir Next.ttc", 18)
    # 3줄로 자르니 문장이 끊겼다 — 4줄까지 두고 줄간격을 줄인다
    block(wrap(T["p1"], f_p, 480, 3), f_p, 83, 614, GREY, 24)
    block(wrap(T["p2"], f_p, 480, 4), f_p, 83, 692, GREY, 24)

    f_c = font("Avenir Next.ttc", 14)
    for cx, label in zip(ICONS, T["checks"]):
        lines = wrap(label, f_c, 172, 3)
        yy = 872
        for ln in lines:
            d.text((cx - d.textlength(ln, font=f_c) / 2, yy), ln, font=f_c, fill=GREEN)
            yy += 18

    d.text((82, 952), " ".join(T["tag"]), font=font("Avenir Next.ttc", 13), fill=GREY)

    p = "%s/about-visual-%s.jpg" % (OUT, lang)
    im.save(p, quality=86, optimize=True)
    print("  %s" % p.split("/")[-1])


for lg in ("en", "id"):
    build(lg)
