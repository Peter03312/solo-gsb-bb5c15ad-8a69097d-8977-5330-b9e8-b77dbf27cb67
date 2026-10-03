"""船舶档案 + 船号预排/确认测试。"""
import json

from conftest import add_node, add_segment, seed_initial_revision


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _simple_world(client, depth=9.0, clearance=None, travel_time=3):
    """A->B 单航段 + 修订 #1（0..59 全程潮位 3m，足够大多数船舶参数）。"""
    add_node(client, "A")
    add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=travel_time,
                      depth=depth, clearance=clearance)
    rev = seed_initial_revision(_level(0, 59, 3.0))
    return seg, rev


def _create_ship(client, number="V-001", draft=5.0, height=8.0, margin=0.5,
                 active=True, note=None, code=201):
    r = client.post("/api/ships", json={
        "ship_number": number, "draft": draft, "height": height,
        "margin": margin, "active": active, "note": note,
    })
    assert r.status_code == code, r.text
    return r.json()


def _plan_body(number=None, rev=None):
    body = {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
    }
    if number is not None:
        body["ship_number"] = number
    else:
        body.update(draft=5.0, height=8.0, margin=0.5)
    if rev is not None:
        body["revision"] = rev
    return body


# ===========================================================================
# 1. 档案管理
# ===========================================================================

class TestShipArchive:
    def test_create_and_get(self, client):
        data = _create_ship(client, "V-001", note="初建")
        assert data["current_version"] == 1
        assert data["active"] is True
        assert (data["draft"], data["height"], data["margin"]) == (5.0, 8.0, 0.5)
        assert len(data["versions"]) == 1
        v = data["versions"][0]
        assert v["version"] == 1 and v["is_current"] is True
        assert v["active"] is True and v["note"] == "初建"
        assert v["created_at"]

    def test_list_summaries(self, client):
        _create_ship(client, "V-002")
        _create_ship(client, "V-001", draft=6.0)
        r = client.get("/api/ships")
        assert r.status_code == 200
        rows = r.json()
        assert [x["ship_number"] for x in rows] == ["V-001", "V-002"]
        assert rows[0]["current_version"] == 1 and rows[0]["draft"] == 6.0
        assert "versions" not in rows[0]

    def test_duplicate_ship_rejected(self, client):
        _create_ship(client, "V-001")
        data = _create_ship(client, "V-001", code=409)
        assert data["error"]["code"] == "SHIP_ALREADY_EXISTS"
        # 不留半成品：仍是版本 1
        assert client.get("/api/ships/V-001").json()["current_version"] == 1

    def test_blank_ship_number_rejected(self, client):
        r = client.post("/api/ships", json={
            "ship_number": "   ", "draft": 1.0, "height": 1.0, "margin": 0.0,
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_negative_parameters_rejected(self, client):
        for field in ("draft", "height", "margin"):
            payload = {"ship_number": "X", "draft": 1.0, "height": 1.0, "margin": 0.0}
            payload[field] = -0.01
            r = client.post("/api/ships", json=payload)
            assert r.status_code == 422, r.text
            assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_zero_parameters_allowed(self, client):
        data = _create_ship(client, "Z-000", draft=0.0, height=0.0, margin=0.0)
        assert (data["draft"], data["height"], data["margin"]) == (0.0, 0.0, 0.0)

    def test_unknown_ship_404(self, client):
        r = client.get("/api/ships/NOPE")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "SHIP_NOT_FOUND"

    def test_create_inactive(self, client):
        data = _create_ship(client, "PARKED", active=False)
        assert data["active"] is False
        assert data["versions"][0]["active"] is False

    def test_version_endpoints(self, client):
        _create_ship(client, "V-001")
        r = client.get("/api/ships/V-001/versions")
        assert r.status_code == 200 and len(r.json()) == 1
        r = client.get("/api/ships/V-001/versions/1")
        assert r.status_code == 200 and r.json()["is_current"] is True

    def test_unknown_version_404(self, client):
        _create_ship(client, "V-001")
        r = client.get("/api/ships/V-001/versions/9")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "SHIP_VERSION_NOT_FOUND"


class TestShipVersions:
    def _add(self, client, number="V-001", **kw):
        kw.setdefault("draft", 6.0)
        kw.setdefault("height", 8.0)
        kw.setdefault("margin", 0.5)
        kw.setdefault("active", True)
        return client.post(f"/api/ships/{number}/versions", json=kw)

    def test_effective_change_increments(self, client):
        _create_ship(client, "V-001")
        r = self._add(client, margin=1.0, note="加大余量")
        assert r.status_code == 201, r.text
        v2 = r.json()
        assert v2["version"] == 2 and v2["is_current"] is True
        assert v2["margin"] == 1.0 and v2["note"] == "加大余量"

        detail = client.get("/api/ships/V-001").json()
        assert detail["current_version"] == 2 and detail["margin"] == 1.0
        assert [v["version"] for v in detail["versions"]] == [1, 2]
        assert detail["versions"][0]["is_current"] is False
        assert detail["versions"][1]["is_current"] is True

    def test_unchanged_rejected_no_partial_version(self, client):
        _create_ship(client, "V-001", draft=5.0, height=8.0, margin=0.5)
        r = self._add(client, draft=5.0, height=8.0, margin=0.5, active=True)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "SHIP_VERSION_UNCHANGED"
        detail = client.get("/api/ships/V-001").json()
        assert detail["current_version"] == 1
        assert len(detail["versions"]) == 1

    def test_toggle_active_is_effective_change(self, client):
        _create_ship(client, "V-001")
        r = self._add(client, draft=5.0, height=8.0, margin=0.5, active=False,
                      note="停用")
        assert r.status_code == 201
        v2 = r.json()
        assert v2["version"] == 2 and v2["active"] is False
        assert client.get("/api/ships/V-001").json()["active"] is False

        # 参数变化但仍停用 -> 版本 3
        r = self._add(client, draft=5.5, active=False)
        assert r.status_code == 201 and r.json()["version"] == 3

        # 重新启用（参数完全同 v3，仅 active 变）-> 版本 4
        r = self._add(client, draft=5.5, active=True)
        assert r.status_code == 201
        v4 = r.json()
        assert v4["version"] == 4 and v4["active"] is True

    def test_negative_in_new_version_rejected(self, client):
        _create_ship(client, "V-001")
        r = self._add(client, height=-1.0)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_add_version_unknown_ship(self, client):
        r = self._add(client, "GHOST")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "SHIP_NOT_FOUND"


# ===========================================================================
# 2. 船号预排
# ===========================================================================

class TestPlanByShip:
    def test_plan_by_ship_returns_reference(self, client):
        _simple_world(client)
        _create_ship(client, "V-001")
        r = client.post("/api/trips/plan", json=_plan_body("V-001"))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ship_number"] == "V-001"
        assert data["ship_version"] == 1
        assert (data["draft"], data["height"], data["margin"]) == (5.0, 8.0, 0.5)
        assert data["arrival_minute"] == 3
        assert data["itinerary"][0]["start_minute"] == 0

    def test_plan_uses_current_enabled_version(self, client):
        _simple_world(client, depth=9.0)
        _create_ship(client, "V-001", draft=5.0)
        # 版本 2：更大吃水（仍然可行）
        client.post("/api/ships/V-001/versions", json={
            "draft": 7.0, "height": 8.0, "margin": 0.5, "active": True,
        })
        r = client.post("/api/trips/plan", json=_plan_body("V-001"))
        assert r.status_code == 200
        assert r.json()["ship_version"] == 2
        assert r.json()["draft"] == 7.0

    def test_plan_unknown_ship(self, client):
        _simple_world(client)
        r = client.post("/api/trips/plan", json=_plan_body("NOPE"))
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "SHIP_NOT_FOUND"

    def test_plan_inactive_ship_rejected(self, client):
        _simple_world(client)
        _create_ship(client, "V-001", active=False)
        r = client.post("/api/trips/plan", json=_plan_body("V-001"))
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "SHIP_INACTIVE"

        # 停用后再启用：新版本 2 生效
        client.post("/api/ships/V-001/versions", json={
            "draft": 5.0, "height": 8.0, "margin": 0.5, "active": True,
        })
        r = client.post("/api/trips/plan", json=_plan_body("V-001"))
        assert r.status_code == 200
        assert r.json()["ship_version"] == 2

    def test_mixed_input_rejected(self, client):
        _simple_world(client)
        _create_ship(client, "V-001")
        body = _plan_body("V-001")
        body["draft"] = 5.0  # 船号 + 参数混用
        r = client.post("/api/trips/plan", json=body)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "MIXED_SHIP_INPUT"

    def test_incomplete_parameters_rejected(self, client):
        _simple_world(client)
        body = {"origin": "A", "destination": "B", "start_minute": 0,
                "window_minutes": 20, "draft": 5.0, "height": 8.0}  # 缺 margin
        r = client.post("/api/trips/plan", json=body)
        assert r.status_code == 422
        err = r.json()["error"]
        assert err["code"] == "SHIP_PARAMETERS_INCOMPLETE"
        assert err["details"]["missing"] == ["margin"]

    def test_neither_ship_nor_parameters_rejected(self, client):
        _simple_world(client)
        body = {"origin": "A", "destination": "B", "start_minute": 0,
                "window_minutes": 20}
        r = client.post("/api/trips/plan", json=body)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "SHIP_PARAMETERS_INCOMPLETE"

    def test_parameter_mode_still_compatible(self, client):
        _simple_world(client)
        r = client.post("/api/trips/plan", json=_plan_body())
        assert r.status_code == 200
        data = r.json()
        assert data["ship_number"] is None and data["ship_version"] is None
        assert data["draft"] == 5.0

    def test_explicit_nulls_count_as_absent(self, client):
        _simple_world(client)
        _create_ship(client, "V-001")
        body = _plan_body("V-001")
        body.update(draft=None, height=None, margin=None)
        r = client.post("/api/trips/plan", json=body)
        assert r.status_code == 200
        assert r.json()["ship_version"] == 1


# ===========================================================================
# 3. 船号确认（事务内核验启用 + 版本未变 + 复算）
# ===========================================================================

class TestConfirmByShip:
    def _plan(self, client, number="V-001"):
        return client.post("/api/trips/plan", json=_plan_body(number)).json()

    def _confirm_body(self, plan, number="V-001", version=1):
        return {
            "origin": plan["origin"], "destination": plan["destination"],
            "start_minute": plan["start_minute"],
            "window_minutes": plan["window_minutes"],
            "ship_number": number, "ship_version": version,
            "revision": plan["revision"], "itinerary": plan["itinerary"],
        }

    def test_confirm_by_ship_success_freezes_reference(self, client):
        _simple_world(client)
        _create_ship(client, "V-001")
        plan = self._plan(client)
        r = client.post("/api/trips/confirm",
                        json=self._confirm_body(plan))
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["ship_number"] == "V-001"
        assert data["ship_version"] == 1
        assert (data["draft"], data["height"], data["margin"]) == (5.0, 8.0, 0.5)

        got = client.get(f"/api/trips/{data['trip_id']}").json()
        assert got["ship_number"] == "V-001" and got["ship_version"] == 1

    def test_confirm_unknown_ship(self, client):
        _simple_world(client)
        # 无档案直接确认
        body = {
            "origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": 20, "ship_number": "GHOST", "ship_version": 1,
            "revision": 1, "itinerary": [],
        }
        r = client.post("/api/trips/confirm", json=body)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "SHIP_NOT_FOUND"

    def test_confirm_requires_version(self, client):
        _simple_world(client)
        _create_ship(client, "V-001")
        plan = self._plan(client)
        body = self._confirm_body(plan)
        del body["ship_version"]
        r = client.post("/api/trips/confirm", json=body)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "SHIP_VERSION_REQUIRED"

    def test_confirm_stale_version_after_archive_change(self, client):
        _simple_world(client)
        _create_ship(client, "V-001")
        plan = self._plan(client)
        # 预排后档案发生有效变更（版本推进）
        client.post("/api/ships/V-001/versions", json={
            "draft": 5.5, "height": 8.0, "margin": 0.5, "active": True,
        })
        r = client.post("/api/trips/confirm",
                        json=self._confirm_body(plan, version=1))
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "SHIP_VERSION_STALE"
        assert err["details"] == {
            "ship_number": "V-001", "provided_version": 1, "current_version": 2,
        }
        # 拒绝且不保存
        assert client.get("/api/trips").json() == []

    def test_confirm_after_change_replan_with_new_version(self, client):
        _simple_world(client)
        _create_ship(client, "V-001")
        self._plan(client)
        client.post("/api/ships/V-001/versions", json={
            "draft": 5.5, "height": 8.0, "margin": 0.5, "active": True,
        })
        plan = client.post("/api/trips/plan", json=_plan_body("V-001")).json()
        assert plan["ship_version"] == 2
        r = client.post("/api/trips/confirm",
                        json=self._confirm_body(plan, version=2))
        assert r.status_code == 201
        assert r.json()["ship_version"] == 2

    def test_confirm_disabled_version_rejected(self, client):
        _simple_world(client)
        _create_ship(client, "V-001")
        plan = self._plan(client)
        # 预排后停用（版本 2 停用）
        client.post("/api/ships/V-001/versions", json={
            "draft": 5.0, "height": 8.0, "margin": 0.5, "active": False,
        })
        r = client.post("/api/trips/confirm",
                        json=self._confirm_body(plan, version=1))
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "SHIP_VERSION_STALE"
        assert client.get("/api/trips").json() == []

    def test_confirm_unknown_version(self, client):
        _simple_world(client)
        _create_ship(client, "V-001")
        plan = self._plan(client)
        r = client.post("/api/trips/confirm",
                        json=self._confirm_body(plan, version=99))
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "SHIP_VERSION_NOT_FOUND"

    def test_confirm_mixed_input_rejected(self, client):
        _simple_world(client)
        _create_ship(client, "V-001")
        plan = self._plan(client)
        body = self._confirm_body(plan)
        body["draft"] = 5.0  # 船号与参数混用
        r = client.post("/api/trips/confirm", json=body)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "MIXED_SHIP_INPUT"
        assert client.get("/api/trips").json() == []

    def test_confirm_ship_version_without_number(self, client):
        _simple_world(client)
        plan = client.post("/api/trips/plan", json=_plan_body()).json()
        body = {
            "origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": 20, "draft": 5.0, "height": 8.0, "margin": 0.5,
            "revision": plan["revision"], "itinerary": plan["itinerary"],
            "ship_version": 1,
        }
        r = client.post("/api/trips/confirm", json=body)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "MIXED_SHIP_INPUT"

    def test_confirm_parameter_mode_still_compatible(self, client):
        _simple_world(client)
        plan = client.post("/api/trips/plan", json=_plan_body()).json()
        body = {
            "origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": 20, "draft": 5.0, "height": 8.0, "margin": 0.5,
            "revision": plan["revision"], "itinerary": plan["itinerary"],
        }
        r = client.post("/api/trips/confirm", json=body)
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["ship_number"] is None and data["ship_version"] is None


# ===========================================================================
# 4. 快照固化 / 历史不改写 / 复核只用快照参数
# ===========================================================================

class TestSnapshotFrozen:
    def test_later_archive_change_keeps_history(self, client):
        _simple_world(client)
        _create_ship(client, "V-001", draft=5.0)
        plan = client.post("/api/trips/plan", json=_plan_body("V-001")).json()
        r = client.post("/api/trips/confirm", json={
            "origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": 20, "ship_number": "V-001", "ship_version": 1,
            "revision": plan["revision"], "itinerary": plan["itinerary"],
        })
        trip_id = r.json()["trip_id"]

        # 档案后续变更：吃水改到极端值、停用
        client.post("/api/ships/V-001/versions", json={
            "draft": 8.8, "height": 8.0, "margin": 0.5, "active": False,
        })
        snap = client.get(f"/api/trips/{trip_id}").json()
        assert snap["draft"] == 5.0
        assert snap["ship_number"] == "V-001" and snap["ship_version"] == 1

        # 复核只用快照参数（不会因档案变化而改变）；无新修订 -> safe
        rev = client.post("/api/trips/{x}/review".format(x=trip_id)).json()
        assert rev["safe"] is True
        assert rev["draft"] == 5.0
        assert rev["ship_number"] == "V-001" and rev["ship_version"] == 1

    def test_review_uses_snapshot_not_current_archive(self, client):
        # 档案版本 2 吃水极大，但快照固化版本 1 的小吃水；潮位复核应按快照判定安全
        _simple_world(client, depth=9.0)
        _create_ship(client, "V-001", draft=5.0)
        plan = client.post("/api/trips/plan", json=_plan_body("V-001")).json()
        c = client.post("/api/trips/confirm", json={
            "origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": 20, "ship_number": "V-001", "ship_version": 1,
            "revision": plan["revision"], "itinerary": plan["itinerary"],
        }).json()
        client.post("/api/ships/V-001/versions", json={
            "draft": 8.9, "height": 8.0, "margin": 0.5, "active": True,
        })
        rev = client.post(f"/api/trips/{c['trip_id']}/review").json()
        assert rev["safe"] is True  # 快照吃水 5.0 始终可行

    def test_parameter_mode_trip_has_null_refs_in_review(self, client):
        _simple_world(client)
        plan = client.post("/api/trips/plan", json=_plan_body()).json()
        c = client.post("/api/trips/confirm", json={
            "origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": 20, "draft": 5.0, "height": 8.0, "margin": 0.5,
            "revision": plan["revision"], "itinerary": plan["itinerary"],
        }).json()
        rev = client.post(f"/api/trips/{c['trip_id']}/review").json()
        assert rev["ship_number"] is None and rev["ship_version"] is None


# ===========================================================================
# 5. 批次：同修订船号预排 / 确认
# ===========================================================================

class TestBatchByShip:
    def _two_independent_routes(self, client):
        # A->B 与 C->D 两条互不干扰航段，便于两艘船同窗口预排
        for n in ("A", "B", "C", "D"):
            add_node(client, n)
        add_segment(client, "A", "B", 3, depth=9.0)
        add_segment(client, "C", "D", 3, depth=9.0)
        seed_initial_revision(_level(0, 59, 3.0))

    def _batch_plan_body(self, numbers=("V-001", "V-002")):
        ships = [
            {"origin": a, "destination": b, "start_minute": 0,
             "window_minutes": 20, "ship_number": n}
            for (a, b), n in zip([("A", "B"), ("C", "D")], numbers)
        ]
        return {"ships": ships}

    def test_batch_plan_by_ship(self, client):
        self._two_independent_routes(client)
        _create_ship(client, "V-001", draft=5.0)
        _create_ship(client, "V-002", draft=6.0)
        r = client.post("/api/trips/batch/plan", json=self._batch_plan_body())
        assert r.status_code == 200, r.text
        ships = r.json()["ships"]
        assert ships[0]["ship_number"] == "V-001" and ships[0]["ship_version"] == 1
        assert ships[1]["ship_number"] == "V-002" and ships[1]["ship_version"] == 1
        assert ships[0]["draft"] == 5.0 and ships[1]["draft"] == 6.0

    def test_batch_plan_mixed_modes_rejected_with_index(self, client):
        self._two_independent_routes(client)
        _create_ship(client, "V-001")
        body = self._batch_plan_body()
        # 第二艘既不给船号也不给参数
        body["ships"][1].pop("ship_number")
        r = client.post("/api/trips/batch/plan", json=body)
        assert r.status_code == 422
        err = r.json()["error"]
        assert err["code"] == "SHIP_PARAMETERS_INCOMPLETE"
        assert err["details"]["ship_index"] == 1

        # 第一艘混用
        body = self._batch_plan_body()
        body["ships"][0]["draft"] = 5.0
        r = client.post("/api/trips/batch/plan", json=body)
        assert r.status_code == 422
        err = r.json()["error"]
        assert err["code"] == "MIXED_SHIP_INPUT"
        assert err["details"]["ship_index"] == 0

    def test_batch_plan_inactive_and_unknown_tagged(self, client):
        self._two_independent_routes(client)
        _create_ship(client, "V-001", active=False)
        _create_ship(client, "V-002")
        r = client.post("/api/trips/batch/plan", json=self._batch_plan_body())
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "SHIP_INACTIVE" and err["details"]["ship_index"] == 0

        body = self._batch_plan_body(("GHOST", "V-002"))
        r = client.post("/api/trips/batch/plan", json=body)
        assert r.status_code == 404
        err = r.json()["error"]
        assert err["code"] == "SHIP_NOT_FOUND" and err["details"]["ship_index"] == 0

    def test_batch_confirm_by_ship_freezes_refs(self, client):
        self._two_independent_routes(client)
        _create_ship(client, "V-001")
        _create_ship(client, "V-002")
        plan = client.post("/api/trips/batch/plan",
                           json=self._batch_plan_body()).json()
        ships = []
        for i, s in enumerate(plan["ships"]):
            ships.append({
                "origin": s["origin"], "destination": s["destination"],
                "start_minute": s["start_minute"],
                "window_minutes": s["window_minutes"],
                "ship_number": s["ship_number"],
                "ship_version": s["ship_version"],
                "itinerary": s["itinerary"],
            })
        body = {"ships": ships, "revision": plan["revision"],
                "idempotency_key": "batch-1"}
        r = client.post("/api/trips/batch/confirm", json=body)
        assert r.status_code == 201, r.text
        trips = r.json()["trips"]
        assert trips[0]["ship_number"] == "V-001" and trips[0]["ship_version"] == 1
        assert trips[1]["ship_number"] == "V-002" and trips[1]["ship_version"] == 1

        listed = {t["trip_id"]: t for t in client.get("/api/trips").json()}
        for t in trips:
            assert listed[t["trip_id"]]["ship_number"] == t["ship_number"]

    def test_batch_confirm_stale_archive_rolls_back_all(self, client):
        self._two_independent_routes(client)
        _create_ship(client, "V-001")
        _create_ship(client, "V-002")
        plan = client.post("/api/trips/batch/plan",
                           json=self._batch_plan_body()).json()
        # 第 2 艘（index 1）档案变更
        client.post("/api/ships/V-002/versions", json={
            "draft": 6.5, "height": 8.0, "margin": 0.5, "active": True,
        })
        ships = [{
            "origin": s["origin"], "destination": s["destination"],
            "start_minute": s["start_minute"], "window_minutes": s["window_minutes"],
            "ship_number": s["ship_number"], "ship_version": s["ship_version"],
            "itinerary": s["itinerary"],
        } for s in plan["ships"]]
        r = client.post("/api/trips/batch/confirm", json={
            "ships": ships, "revision": plan["revision"],
            "idempotency_key": "batch-stale",
        })
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "SHIP_VERSION_STALE"
        assert err["details"]["ship_index"] == 1
        # 批次全退：无任何快照
        assert client.get("/api/trips").json() == []

    def test_batch_confirm_inactive_rolls_back_all(self, client):
        self._two_independent_routes(client)
        _create_ship(client, "V-001")
        _create_ship(client, "V-002")
        plan = client.post("/api/trips/batch/plan",
                           json=self._batch_plan_body()).json()
        # 停用第二艘（新版本，停用）
        client.post("/api/ships/V-002/versions", json={
            "draft": 6.0, "height": 8.0, "margin": 0.5, "active": False,
        })
        ships = [{
            "origin": s["origin"], "destination": s["destination"],
            "start_minute": s["start_minute"], "window_minutes": s["window_minutes"],
            "ship_number": s["ship_number"], "ship_version": s["ship_version"],
            "itinerary": s["itinerary"],
        } for s in plan["ships"]]
        r = client.post("/api/trips/batch/confirm", json={
            "ships": ships, "revision": plan["revision"],
            "idempotency_key": "batch-inactive",
        })
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "SHIP_VERSION_STALE"
        assert err["details"]["ship_index"] == 1
        assert client.get("/api/trips").json() == []

    def test_batch_confirm_mixed_modes_rejected(self, client):
        self._two_independent_routes(client)
        _create_ship(client, "V-001")
        plan = client.post("/api/trips/batch/plan",
                           json=self._batch_plan_body(("V-001", "V-001"))).json()
        ships = [{
            "origin": s["origin"], "destination": s["destination"],
            "start_minute": 0, "window_minutes": 20,
            "ship_number": "V-001", "ship_version": 1,
            "draft": 5.0,  # 船号 + 参数混用
            "itinerary": s["itinerary"],
        } for s in plan["ships"]]
        r = client.post("/api/trips/batch/confirm", json={
            "ships": ships, "revision": plan["revision"],
            "idempotency_key": "batch-mixed",
        })
        assert r.status_code == 422
        err = r.json()["error"]
        assert err["code"] == "MIXED_SHIP_INPUT"
        assert err["details"]["ship_index"] == 0
        assert client.get("/api/trips").json() == []

    def test_batch_confirm_requires_ship_version(self, client):
        self._two_independent_routes(client)
        _create_ship(client, "V-001")
        _create_ship(client, "V-002")
        plan = client.post("/api/trips/batch/plan",
                           json=self._batch_plan_body()).json()
        ships = [{
            "origin": s["origin"], "destination": s["destination"],
            "start_minute": s["start_minute"], "window_minutes": s["window_minutes"],
            "ship_number": s["ship_number"],
            "itinerary": s["itinerary"],
        } for s in plan["ships"]]  # 均缺 ship_version
        r = client.post("/api/trips/batch/confirm", json={
            "ships": ships, "revision": plan["revision"],
            "idempotency_key": "batch-nover",
        })
        assert r.status_code == 422
        err = r.json()["error"]
        assert err["code"] == "SHIP_VERSION_REQUIRED"
        assert err["details"]["ship_index"] == 0

    def test_batch_modes_can_coexist_within_batch(self, client):
        # 同批内第一艘用船号、第二艘用参数（逐船二选一）：允许
        self._two_independent_routes(client)
        _create_ship(client, "V-001", draft=5.0)
        body = {
            "ships": [
                {"origin": "A", "destination": "B", "start_minute": 0,
                 "window_minutes": 20, "ship_number": "V-001"},
                {"origin": "C", "destination": "D", "start_minute": 0,
                 "window_minutes": 20, "draft": 6.0, "height": 8.0, "margin": 0.5},
            ]
        }
        r = client.post("/api/trips/batch/plan", json=body)
        assert r.status_code == 200, r.text
        ships = r.json()["ships"]
        assert ships[0]["ship_number"] == "V-001"
        assert ships[1]["ship_number"] is None and ships[1]["draft"] == 6.0

    def test_batch_replay_after_archive_change_uses_old_semantics(self, client):
        """作废与批次同键重放沿用旧语义：档案变更后同键同内容重放仍返回首次结果。"""
        self._two_independent_routes(client)
        _create_ship(client, "V-001")
        _create_ship(client, "V-002")
        plan_body = self._batch_plan_body()
        plan = client.post("/api/trips/batch/plan", json=plan_body).json()
        ships = [{
            "origin": s["origin"], "destination": s["destination"],
            "start_minute": s["start_minute"], "window_minutes": s["window_minutes"],
            "ship_number": s["ship_number"], "ship_version": s["ship_version"],
            "itinerary": s["itinerary"],
        } for s in plan["ships"]]
        payload = {"ships": ships, "revision": plan["revision"],
                   "idempotency_key": "batch-replay"}
        first = client.post("/api/trips/batch/confirm", json=payload).json()
        first_ids = [t["trip_id"] for t in first["trips"]]

        # 档案全部推进新版本后，同键同内容重放
        for n in ("V-001", "V-002"):
            client.post(f"/api/ships/{n}/versions", json={
                "draft": 5.5, "height": 8.0, "margin": 0.5, "active": True,
            })
        r = client.post("/api/trips/batch/confirm", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert data["replayed"] is True
        assert [t["trip_id"] for t in data["trips"]] == first_ids
        # 仍是首次固化的版本号引用
        assert [t["ship_version"] for t in data["trips"]] == [1, 1]
        # 未新增快照
        assert len(client.get("/api/trips").json()) == 2


# ===========================================================================
# 6. 端到端：档案版本 1 规划 → 版本 2 改参数 → 旧规划确认被拒 → 重排确认
# ===========================================================================

class TestEndToEnd:
    def test_full_flow(self, client):
        # depth=6.0、潮位 3.0：v1 吃水 8.4 可行（6+3 >= 8.4+0.5=8.9），
        # v2 吃水 8.6 不可行（需 9.1 > 9.0）
        _simple_world(client, depth=6.0)
        _create_ship(client, "V-001", draft=8.4, height=8.0, margin=0.5)

        plan = client.post("/api/trips/plan", json=_plan_body("V-001")).json()
        assert plan["ship_version"] == 1
        assert plan["arrival_minute"] == 3

        # 有效变更：加深吃水至 8.6（+0.5 余量需 9.1 > 9.0，窗口无可行行程）
        client.post("/api/ships/V-001/versions", json={
            "draft": 8.6, "height": 8.0, "margin": 0.5, "active": True,
        })

        # 旧规划确认：版本已推进 -> SHIP_VERSION_STALE
        r = client.post("/api/trips/confirm", json={
            "origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": 20, "ship_number": "V-001", "ship_version": 1,
            "revision": plan["revision"], "itinerary": plan["itinerary"],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "SHIP_VERSION_STALE"
        assert client.get("/api/trips").json() == []

        # 以当前版本重排 -> 当前参数下窗口无可行行程
        r = client.post("/api/trips/plan", json=_plan_body("V-001"))
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NO_FEASIBLE_ITINERARY"

        # 再登记版本 3 把吃水改回 -> 预排与确认成功，固化版本 3
        client.post("/api/ships/V-001/versions", json={
            "draft": 8.4, "height": 8.0, "margin": 0.5, "active": True,
        })
        plan3 = client.post("/api/trips/plan", json=_plan_body("V-001")).json()
        assert plan3["ship_version"] == 3
        r = client.post("/api/trips/confirm", json={
            "origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": 20, "ship_number": "V-001", "ship_version": 3,
            "revision": plan3["revision"], "itinerary": plan3["itinerary"],
        })
        assert r.status_code == 201, r.text
        assert r.json()["ship_version"] == 3

        # 档案再停用不影响历史快照读取
        client.post("/api/ships/V-001/versions", json={
            "draft": 8.4, "height": 8.0, "margin": 0.5, "active": False,
        })
        trips = client.get("/api/trips").json()
        assert len(trips) == 1 and trips[0]["ship_version"] == 3
