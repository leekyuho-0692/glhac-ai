"""국어의 로마자 표기법(문화체육관광부 고시) 기반 한글 → 로마자 변환.

왜 필요한가: 한글로 들어온 담당자·감독자 이름이 인니·영문 서류에 그대로 나가면 심사자가 읽지
못한다. 표기법은 규정이 있어 결정적으로 옮길 수 있으므로 업체마다 사전을 만들 필요가 없다.

한계(중요): 이것은 '표기 변환'이지 번역이 아니다.
  · 회사명·상표는 로마자 표기와 실제 영문 상호가 다를 수 있다
    (바이오로제트 → 표기 Baiorojeteu / 실제 상호 BIOROSETTE CO., LTD.).
    그래서 회사명은 자동값을 확정으로 쓰지 않고 입력 제안으로만 쓴다.
  · 음운 변동(자음동화 등)은 인명에서 붙임표로 끊어 적는 관행을 따라 적용하지 않는다
    (고시 제3장 제4항: 인명은 음절 사이 음운 변화를 표기에 반영하지 않는다).
"""

_CHO = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj",
        "ch", "k", "t", "p", "h"]
_JUNG = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo",
         "u", "wo", "we", "wi", "yu", "eu", "ui", "i"]
_JONG = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l",
         "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t", "p", "t"]
_BASE = 0xAC00
_LAST = 0xD7A3


def _syllable(ch):
    """한글 음절 → (초성, 중성, 종성) 로마자. 한글이 아니면 None."""
    o = ord(ch)
    if not (_BASE <= o <= _LAST):
        return None
    i = o - _BASE
    return _CHO[i // 588], _JUNG[(i % 588) // 28], _JONG[i % 28]


def romanize(text, capitalize=True):
    """한글 문자열 → 로마자. 한글이 아닌 문자는 그대로 둔다."""
    if not text:
        return text
    out, buf = [], []
    for ch in text:
        s = _syllable(ch)
        if s:
            buf.append("".join(s))
            continue
        if buf:
            w = "".join(buf)
            out.append(w.capitalize() if capitalize else w)
            buf = []
        out.append(ch)
    if buf:
        w = "".join(buf)
        out.append(w.capitalize() if capitalize else w)
    return "".join(out)


# 성씨 관용 표기 — 고시도 성씨는 관용을 허용한다(김 Gim → 여권·서류는 Kim).
# 특정 업체가 아니라 한국인 전체에 적용되는 일반 규칙이므로 코드에 두는 것이 맞다.
_SURNAME = {
    "김": "Kim", "이": "Lee", "박": "Park", "최": "Choi", "정": "Jung", "강": "Kang",
    "조": "Cho", "윤": "Yoon", "장": "Jang", "임": "Lim", "한": "Han", "오": "Oh",
    "서": "Seo", "신": "Shin", "권": "Kwon", "황": "Hwang", "안": "Ahn", "송": "Song",
    "전": "Jeon", "홍": "Hong", "유": "Yoo", "류": "Ryu", "고": "Ko", "문": "Moon",
    "손": "Son", "양": "Yang", "배": "Bae", "백": "Baek", "허": "Heo", "남": "Nam",
    "심": "Sim", "노": "Noh", "하": "Ha", "곽": "Kwak", "성": "Sung", "차": "Cha",
    "주": "Joo", "우": "Woo", "구": "Koo", "민": "Min", "나": "Na", "진": "Jin",
    "지": "Ji", "엄": "Eom", "채": "Chae", "원": "Won", "천": "Chun", "방": "Bang",
    "공": "Kong", "현": "Hyun", "함": "Ham", "변": "Byun", "염": "Yeom", "여": "Yeo",
    "추": "Chu", "도": "Do", "소": "So", "석": "Seok", "선": "Sun", "설": "Seol",
    "마": "Ma", "길": "Gil", "연": "Yeon", "위": "Wi", "표": "Pyo", "명": "Myung",
    "기": "Ki", "반": "Ban", "라": "Ra", "왕": "Wang", "금": "Keum", "옥": "Ok",
    "육": "Yook", "인": "In", "맹": "Maeng", "제": "Je", "모": "Mo", "탁": "Tak",
    "국": "Kook", "어": "Eo", "은": "Eun", "편": "Pyun", "용": "Yong", "예": "Ye",
}
_SURNAME2 = {"남궁": "Namgoong", "제갈": "Jegal", "선우": "Sunwoo", "독고": "Dokgo",
             "황보": "Hwangbo", "사공": "Sagong", "서문": "Seomun"}
# 이름 음절 관용 표기 — 표기법대로면 Yeong-hui·Dae-hyeon이지만 여권·명함은 Young-hee·Dae-hyun을 쓴다.
# 성씨와 마찬가지로 한국인 전체에 적용되는 일반 규칙이라 코드에 둔다(업체별 사전이 아니다).
_GIVEN = {
    "영": "Young", "현": "Hyun", "우": "Woo", "희": "Hee", "준": "Joon", "성": "Sung",
    "경": "Kyung", "정": "Jung", "선": "Sun", "훈": "Hoon", "승": "Seung", "용": "Yong",
    "규": "Kyu", "근": "Keun", "국": "Kook", "수": "Soo", "숙": "Sook", "순": "Soon",
    "은": "Eun", "윤": "Yoon", "주": "Joo", "철": "Chul", "웅": "Woong", "빈": "Bin",
    "찬": "Chan", "환": "Hwan", "관": "Kwan", "광": "Kwang", "권": "Kwon", "균": "Kyun",
    "복": "Bok", "봉": "Bong", "충": "Choong", "춘": "Choon", "육": "Yook", "율": "Yul",
    "옥": "Ok", "욱": "Wook", "혁": "Hyuk", "협": "Hyup", "형": "Hyung", "홍": "Hong",
    "구": "Koo", "군": "Kun", "궁": "Kung", "극": "Keuk", "긍": "Keung",
}


def romanize_name(name):
    """인명 → '성 이름' 로마자. 한국 인명은 성 1자(드물게 2자) + 이름을 붙임표로 잇는다.
    예) 홍길동 → Hong Gil-dong, 남궁민수 → Namgung Min-su(2자 성은 사전이 필요해 미지원)."""
    if not name:
        return name
    t = name.strip()
    if not all(_syllable(c) for c in t.replace(" ", "")) or " " in t:
        # 공백이 있거나 한글이 아닌 글자가 섞이면 일반 변환으로 처리(추정하지 않는다)
        return romanize(t)
    if len(t) < 2:
        return romanize(t)
    # 두 자 성은 4자 이상일 때만 적용한다. 3자 이름은 '황보라'처럼 갈리는데
    # (황보+라 / 황+보라) 한 자 성이 압도적으로 흔하므로 그쪽을 기본으로 둔다.
    if len(t) >= 4 and t[:2] in _SURNAME2:
        surname, rest = _SURNAME2[t[:2]], t[2:]
    else:
        surname, rest = _SURNAME.get(t[0], romanize(t[0])), t[1:]
    if not rest:
        return surname
    # 이름은 첫 음절만 대문자, 뒤 음절은 소문자로 잇는다(Gil-dong · Chul-soo).
    given = [(_GIVEN.get(c) or romanize(c)).lower() for c in rest]
    given[0] = given[0].capitalize()
    return "%s %s" % (surname, "-".join(given))
