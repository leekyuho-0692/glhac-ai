# -*- coding: utf-8 -*-
"""인트로 효과음 — 실서버(glhac.com) 기준 동작 검증.

브라우저는 사용자가 한 번도 누르지 않은 페이지의 소리를 막는다. 그래서
'자동재생 허용' 조건에서만 확인하면 실제로는 아무 소리도 안 나는 걸 못 잡는다.
여기서는 **차단이 켜진 기본 정책**을 기준으로 본다.

실행:  pytest tests/test_intro_sound.py -q
"""
import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

URL = "https://glhac.com/"
BLOCKED = "document-user-activation-required"   # 실제 브라우저 기본값
ALLOWED = "no-user-gesture-required"

PROBE = """
  window.__r = [];
  const OP = Audio.prototype.play;
  Audio.prototype.play = function(){
    var n = this.src.split('/').pop();
    var v = document.getElementById('intro');
    var vt = v ? +v.currentTime.toFixed(2) : -1;
    var p = OP.apply(this, arguments);
    if (p && p.then) p.then(() => window.__r.push({f:n, ok:true,  vt:vt}),
                            () => window.__r.push({f:n, ok:false, vt:vt}));
    return p;
  };
"""


def visit(policy, act, tz="Asia/Seoul", lang="ko-KR"):
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--autoplay-policy=" + policy])
        ctx = b.new_context(viewport={"width": 1280, "height": 800},
                            timezone_id=tz, locale=lang)
        ctx.add_init_script(PROBE)
        p = ctx.new_page()
        errs, http_bad = [], []
        p.on("pageerror", lambda e: errs.append(str(e)))
        p.on("response", lambda r: http_bad.append((r.status, r.url))
             if r.status >= 400 and "/intro/" in r.url else None)
        p.goto(URL, wait_until="domcontentloaded")
        act(p)
        played = p.evaluate("window.__r")
        btn = p.evaluate("(()=>{var b=document.getElementById('introSoundBtn');"
                         "return b?getComputedStyle(b).display:'none'})()")
        b.close()
        return played, btn, errs, http_bad


def ok_names(played):
    return {e["f"] for e in played if e["ok"]}


def test_자산이_404가_아니다():
    played, _, errs, bad = visit(ALLOWED, lambda p: p.wait_for_timeout(7000))
    assert not bad, "인트로 자산 응답 오류: %s" % bad
    assert not errs, "자바스크립트 오류: %s" % errs


def test_차단되면_소리켜기_버튼이_나온다():
    played, btn, errs, _ = visit(BLOCKED, lambda p: p.wait_for_timeout(7000))
    assert ok_names(played) == set(), "차단 조건인데 소리가 났다: %s" % played
    assert btn != "none", "소리가 막혔는데 '소리 켜기' 버튼이 안 보인다"
    assert not errs


def test_화면을_누르면_소리가_풀린다():
    played, btn, errs, _ = visit(
        BLOCKED, lambda p: (p.wait_for_timeout(1000),
                            p.mouse.click(640, 300),
                            p.wait_for_timeout(6000)))
    names = ok_names(played)
    assert "door-creak2.mp3" in names, "문 소리가 안 났다: %s" % played
    assert {"gong2.mp3"} & names, "악기 소리가 안 났다: %s" % played
    assert btn == "none", "소리가 났는데 버튼이 남아 있다"
    assert not errs


def test_소리켜기_버튼이_실제로_동작한다():
    played, btn, errs, _ = visit(
        BLOCKED, lambda p: (p.wait_for_timeout(6000),
                            p.click("#introSoundBtn"),
                            p.wait_for_timeout(2500)))
    names = ok_names(played)
    assert "gong2.mp3" in names, "버튼을 눌렀는데 소리가 안 난다: %s" % played
    # 같은 소리가 두 번 울리면(버튼 + 전역 리스너) 겹쳐 들린다
    hits = [e for e in played if e["f"] == "gong2.mp3" and e["ok"]]
    assert len(hits) == 1, "악기 소리가 %d번 겹쳐 울렸다" % len(hits)
    assert not errs


def test_문소리는_영상과_동시에_울린다():
    """영상이 돌기 시작하는 순간에 나야 한다. 늦으면 화면과 따로 논다."""
    played, _, errs, _ = visit(ALLOWED, lambda p: p.wait_for_timeout(7000))
    door = [e for e in played if e["f"] == "door-creak2.mp3" and e["ok"]]
    assert door, "문 소리가 안 났다: %s" % played
    assert door[0]["vt"] <= 0.6, "문 소리가 영상 %.2f초에 울렸다(기대 0.6초 이내)" % door[0]["vt"]
    assert not errs


@pytest.mark.parametrize("tz,lang,want", [
    ("Asia/Seoul",       "ko-KR", "gong2.mp3"),
    ("Asia/Jakarta",     "id-ID", "gamelan2.mp3"),
    ("America/New_York", "en-US", "tadum2.mp3"),
    ("Europe/Berlin",    "de-DE", "tadum2.mp3"),
])
def test_나라별_악기(tz, lang, want):
    played, _, errs, _ = visit(ALLOWED, lambda p: p.wait_for_timeout(7000), tz, lang)
    names = ok_names(played)
    assert want in names, "%s 에서 %s 가 아니라 %s" % (tz, want, names)
    assert not errs
