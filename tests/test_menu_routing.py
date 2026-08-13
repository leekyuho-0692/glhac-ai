"""메뉴는 자기 화면으로 가야 한다 — 라우팅 배선 검증.

배경: 오디터가 좌측 '보고서'를 누르면 파트와(Fatwa) 심의 화면이 떴다. 역할 배정은
처음부터 맞았다(REPORT=오디터·컨설턴트 / FATWA=샤리아·운영). route_path만 fatwa로
남아 있었다.

실행: <venv>/bin/python -m pytest tests/test_menu_routing.py -q
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_menuroute_test.db")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as m  # noqa: E402
import app.models as models  # noqa: E402

SEED = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "menu_seed.json")


def _menus():
    return {x["menu_code"]: x for x in json.load(open(SEED, encoding="utf-8"))["menus"]}


def test_report_menu_points_at_the_report_screen():
    """'보고서'가 파트와 화면으로 가던 것 — 시드가 되돌아가면 안 된다."""
    assert _menus()["REPORT"]["route"] == "auditReport"


def test_no_two_menus_share_a_route():
    """서로 다른 메뉴가 같은 화면을 가리키면 하나는 갈 곳을 잃은 것이다."""
    routes = {}
    for code, x in _menus().items():
        if x.get("type") != "screen" or not x.get("route"):
            continue
        assert x["route"] not in routes, "%s ↔ %s 가 %s 공유" % (code, routes[x["route"]], x["route"])
        routes[x["route"]] = code


def test_no_sort_collision_within_a_role():
    """메뉴 순서는 '역할별 배정 sort'가 정한다 — default_sort_order가 아니다(/me/menus).

    그래서 서로 다른 역할에 가는 메뉴끼리 번호가 겹치는 건 문제가 아니다(REPORT·FATWA).
    같은 역할이 한 그룹에서 같은 번호를 두 번 받을 때만 순서가 흔들린다.
    실측: ops·admin이 GRP_1에서 보완·재전송과 동반자 워크스페이스를 둘 다 3번으로 받았다."""
    d = json.load(open(SEED, encoding="utf-8"))
    parent = {x["menu_code"]: x.get("parent") for x in d["menus"]}
    seen = {}
    for rm in d["role_menu"]:
        k = (rm["role"], parent.get(rm["menu_code"]), rm["sort"])
        assert k not in seen, "%s: %s ↔ %s 가 %s 에서 순서 %s 중복" % (
            rm["role"], rm["menu_code"], seen[k], k[1], k[2])
        seen[k] = rm["menu_code"]


def test_repair_rewrites_only_the_wrong_route(tmp_path):
    """운영 DB는 시드를 건너뛰므로 교정이 돈다. 잘못 배선된 것만 고친다."""
    eng = create_engine("sqlite:///%s" % (tmp_path / "menu.db"))
    models.Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    db.add(models.SysMenu(menu_id="m_rep", menu_code="REPORT", menu_type="screen",
                          route_path="fatwa", default_sort_order=6))
    db.add(models.SysMenu(menu_id="m_fat", menu_code="FATWA", menu_type="screen",
                          route_path="fatwa", default_sort_order=6))
    db.commit()
    m._fix_report_route(db)
    assert db.get(models.SysMenu, "m_rep").route_path == "auditReport"
    assert db.get(models.SysMenu, "m_fat").route_path == "fatwa", "파트와 메뉴를 건드렸다"
    assert db.get(models.SysMenu, "m_fat").default_sort_order == 6, "정렬은 손대지 않는다"


def test_repair_respects_a_deliberate_choice(tmp_path):
    """사람이 다른 화면을 지정해 뒀으면 덮지 않는다 — 교정은 알려진 오배선에만."""
    eng = create_engine("sqlite:///%s" % (tmp_path / "menu2.db"))
    models.Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    db.add(models.SysMenu(menu_id="m_rep", menu_code="REPORT", menu_type="screen",
                          route_path="documents", default_sort_order=6))
    db.commit()
    m._fix_report_route(db)
    assert db.get(models.SysMenu, "m_rep").route_path == "documents"


def test_repair_is_idempotent(tmp_path):
    """두 번 돌려도 같다."""
    eng = create_engine("sqlite:///%s" % (tmp_path / "menu3.db"))
    models.Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    db.add(models.SysMenu(menu_id="m_rep", menu_code="REPORT", menu_type="screen",
                          route_path="fatwa", default_sort_order=6))
    db.commit()
    m._fix_report_route(db)
    m._fix_report_route(db)
    assert db.get(models.SysMenu, "m_rep").route_path == "auditReport"


def test_every_seeded_route_has_a_screen():
    """시드가 가리키는 화면이 프런트에 실제로 있어야 한다 — 없으면 빈 화면으로 떨어진다."""
    html = open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "app", "static", "index.html"), encoding="utf-8").read()
    for code, x in _menus().items():
        if x.get("type") != "screen" or not x.get("route"):
            continue
        assert re.search(r"SCREEN\.%s\s*=" % re.escape(x["route"]), html), \
            "%s → SCREEN.%s 없음" % (code, x["route"])
