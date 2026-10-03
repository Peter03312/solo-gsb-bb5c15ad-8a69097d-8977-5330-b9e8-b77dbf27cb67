"""船舶档案（按唯一船号管理的版本化船舶参数）。

调度员按**唯一船号**管理船舶档案：吃水（非负）、水面以上高度（非负）、
安全余量（非负）及启停状态。

* **版本化**：建档产生版本 1；之后仅在吃水 / 水面以上高度 / 安全余量 /
  启停状态至少一项相对**当前版本**发生变化时才允许新增版本（版本号严格
  递增，当前版本 = 最大版本号）；与当前版本完全相同的提交被拒绝
  （``SHIP_VERSION_UNCHANGED``），不留半成品版本。历史版本全部可查。
* **启停**：停用状态属于版本内容——停用可通过提交一个 ``active=false`` 的
  新版本生效（状态切换本身即有效变更）；档案停用期间预排/确认一律拒绝
  （``SHIP_INACTIVE``），其**历史已确认快照不受影响**。
* **预排（只读）**：请求可只给船号，服务按该船**当前启用版本**回填三项
  参数，响应回传船号与版本号；直接提交三项参数的既有方式保持兼容。
  两种输入**禁止混用**（``MIXED_SHIP_INPUT``），三项参数给不全
  ``SHIP_PARAMETERS_INCOMPLETE``。
* **确认（事务内）**：档案模式必须回填船号与预排时的版本号；服务在
  **原确认事务**内核对该船仍存在、该版本仍是当前版本且处于启用状态，
  再按版本参数连同潮位、航时管制与占用一并复算。档案期间发生过有效变更
  （版本推进）或已停用 -> ``SHIP_VERSION_STALE`` / ``SHIP_INACTIVE``，
  拒绝且不保存（批次全批回滚）。
* **快照固化**：确认成功后，三项参数与档案引用（船号、版本号）一并固化
  进 ``confirmed_trips``；后续潮位复核、改期建议只用快照参数，档案后续
  变更不改写历史。
"""
import sqlite3
from typing import Optional

from ..errors import ApiError


PARAM_FIELDS = ("draft", "height", "margin")


# ---- 错误构造 --------------------------------------------------------------------

def ship_not_found(ship_number: str) -> ApiError:
    return ApiError(
        404, "SHIP_NOT_FOUND", f"未知船号: {ship_number}", {"ship_number": ship_number}
    )


def _ship_version_not_found(ship_number: str, version: int) -> ApiError:
    return ApiError(
        404, "SHIP_VERSION_NOT_FOUND",
        f"船舶 {ship_number} 不存在版本 #{version}",
        {"ship_number": ship_number, "version": version},
    )


def _mixed_error(kind: str) -> ApiError:
    return ApiError(
        422, "MIXED_SHIP_INPUT",
        "船号模式与参数模式禁止混用：要么只给 ship_number（由档案回填三项参数），"
        "要么给齐 draft/height/margin，二者不得同时出现",
        {"reason": kind},
    )


def _incomplete_params(missing: list[str]) -> ApiError:
    return ApiError(
        422, "SHIP_PARAMETERS_INCOMPLETE",
        f"参数模式必须同时给齐 draft/height/margin，缺少: {missing}",
        {"missing": missing},
    )


# ---- 建档 / 版本 ----------------------------------------------------------------

def create_ship(conn, ship_number: str, draft: float, height: float,
                margin: float, active: bool, note: Optional[str]) -> dict:
    """按唯一船号建档并写入版本 1。

    船号已存在 -> 409 SHIP_ALREADY_EXISTS；重复时不写入任何内容。
    返回含单版本列表的完整档案（与 GET 详情同构）。
    """
    if conn.execute(
        "SELECT 1 FROM ships WHERE ship_number = ?", (ship_number,)
    ).fetchone() is not None:
        raise ApiError(
            409, "SHIP_ALREADY_EXISTS", f"船号已存在: {ship_number}",
            {"ship_number": ship_number},
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        try:
            conn.execute(
                "INSERT INTO ships(ship_number) VALUES (?)", (ship_number,)
            )
            conn.execute(
                "INSERT INTO ship_versions"
                " (ship_number, version, draft, height, margin, active, note)"
                " VALUES (?, 1, ?, ?, ?, ?, ?)",
                (ship_number, draft, height, margin, 1 if active else 0, note),
            )
        except sqlite3.IntegrityError:
            # 并发建档竞争：另一事务已占用该船号
            conn.execute("ROLLBACK")
            raise ApiError(
                409, "SHIP_ALREADY_EXISTS", f"船号已存在: {ship_number}",
                {"ship_number": ship_number},
            )
        conn.execute("COMMIT")
    except ApiError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_ship(conn, ship_number)


def add_version(conn, ship_number: str, draft: float, height: float,
                margin: float, active: bool, note: Optional[str]) -> dict:
    """为档案登记一个新版本（单事务）。

    未知船号 -> 404 SHIP_NOT_FOUND；新内容与当前版本完全相同（含启停）->
    422 SHIP_VERSION_UNCHANGED，不写新版本。返回新版本（is_current=true）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute(
            "SELECT 1 FROM ships WHERE ship_number = ?", (ship_number,)
        ).fetchone() is None:
            raise ship_not_found(ship_number)

        current = conn.execute(
            "SELECT version, draft, height, margin, active FROM ship_versions"
            " WHERE ship_number = ? ORDER BY version DESC LIMIT 1",
            (ship_number,),
        ).fetchone()
        new_active = 1 if active else 0
        if (current is not None
                and current["draft"] == draft
                and current["height"] == height
                and current["margin"] == margin
                and current["active"] == new_active):
            raise ApiError(
                422, "SHIP_VERSION_UNCHANGED",
                f"船舶 {ship_number} 的吃水/水面以上高度/安全余量/启停状态与当前"
                f"版本 #{current['version']} 完全相同，不产生新版本",
                {"ship_number": ship_number, "current_version": current["version"]},
            )

        next_version = 1 if current is None else current["version"] + 1
        conn.execute(
            "INSERT INTO ship_versions"
            " (ship_number, version, draft, height, margin, active, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ship_number, next_version, draft, height, margin, new_active, note),
        )
        conn.execute("COMMIT")
    except ApiError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_version(conn, ship_number, next_version)


# ---- 查询 -----------------------------------------------------------------------

def _version_out(row, *, current_version: int) -> dict:
    return {
        "ship_number": row["ship_number"],
        "version": row["version"],
        "draft": row["draft"],
        "height": row["height"],
        "margin": row["margin"],
        "active": bool(row["active"]),
        "note": row["note"],
        "created_at": row["created_at"],
        "is_current": row["version"] == current_version,
    }


def list_versions(conn, ship_number: str) -> list[dict]:
    """列出档案全部版本（版本号升序）。未知船号 -> 404 SHIP_NOT_FOUND。"""
    if conn.execute(
        "SELECT 1 FROM ships WHERE ship_number = ?", (ship_number,)
    ).fetchone() is None:
        raise ship_not_found(ship_number)
    rows = conn.execute(
        "SELECT * FROM ship_versions WHERE ship_number = ? ORDER BY version",
        (ship_number,),
    ).fetchall()
    current_version = rows[-1]["version"]
    return [_version_out(r, current_version=current_version) for r in rows]


def get_version(conn, ship_number: str, version: int) -> dict:
    """读取档案指定版本。未知船号 404 SHIP_NOT_FOUND；未知版本
    404 SHIP_VERSION_NOT_FOUND。"""
    versions = list_versions(conn, ship_number)
    for v in versions:
        if v["version"] == version:
            return v
    raise _ship_version_not_found(ship_number, version)


def get_ship(conn, ship_number: str) -> dict:
    """读取档案详情（当前版本 + 全部历史版本）。未知船号 404 SHIP_NOT_FOUND。"""
    row = conn.execute(
        "SELECT ship_number, created_at FROM ships WHERE ship_number = ?",
        (ship_number,),
    ).fetchone()
    if row is None:
        raise ship_not_found(ship_number)
    versions = list_versions(conn, ship_number)
    current = versions[-1]
    return {
        "ship_number": row["ship_number"],
        "created_at": row["created_at"],
        "current_version": current["version"],
        "active": current["active"],
        "draft": current["draft"],
        "height": current["height"],
        "margin": current["margin"],
        "note": current["note"],
        "versions": versions,
    }


def list_ships(conn) -> list[dict]:
    """列出全部船舶档案（按船号排序，仅含当前版本摘要）。"""
    rows = conn.execute(
        "SELECT ship_number FROM ships ORDER BY ship_number"
    ).fetchall()
    summaries = []
    for r in rows:
        detail = get_ship(conn, r["ship_number"])
        summaries.append({k: detail[k] for k in (
            "ship_number", "created_at", "current_version", "active",
            "draft", "height", "margin", "note",
        )})
    return summaries


# ---- 预排 / 确认输入解析 ----------------------------------------------------------

def _params_present(draft, height, margin) -> list[str]:
    """返回三项参数中实际给出（非 None）的字段名。"""
    return [name for name, val in zip(PARAM_FIELDS, (draft, height, margin))
            if val is not None]


def resolve_for_plan(conn, *, ship_number: Optional[str], draft, height, margin
                     ) -> dict:
    """解析预排输入（只读，在确认事务之外亦可调用）。

    返回 {"ship_number", "ship_version", "draft", "height", "margin"}：
    档案模式后两项引用为船号/当前版本号，参数模式引用均为 None。
    """
    present = _params_present(draft, height, margin)
    if ship_number is not None:
        if present:
            raise _mixed_error("ship_number_with_parameters")
        return _load_current(conn, ship_number)

    if present:
        missing = [name for name in PARAM_FIELDS if name not in present]
        if missing:
            raise _incomplete_params(missing)
    else:
        raise _incomplete_params(list(PARAM_FIELDS))
    return {"ship_number": None, "ship_version": None,
            "draft": draft, "height": height, "margin": margin}


def resolve_for_confirm(conn, *, ship_number: Optional[str],
                        ship_version: Optional[int],
                        draft, height, margin) -> dict:
    """解析确认输入。必须在**确认事务内**调用，以保证核对与复算的原子性。

    * 档案模式（给 ship_number）：必须给 ship_version；该版本须存在、仍是
      当前版本且处于启用状态——版本已推进 -> 409 SHIP_VERSION_STALE，
      已停用 -> 409 SHIP_INACTIVE；
    * 参数模式（既有、兼容）：三项参数须给齐；
    * ship_version 无 ship_number、或船号与参数同现 -> 422 MIXED_SHIP_INPUT。
    """
    present = _params_present(draft, height, margin)

    if ship_number is not None:
        if present:
            raise _mixed_error("ship_number_with_parameters")
        if ship_version is None:
            raise ApiError(
                422, "SHIP_VERSION_REQUIRED",
                "档案模式确认必须回填预排响应中的 ship_version",
                {"ship_number": ship_number},
            )
        return _load_for_confirm(conn, ship_number, ship_version)

    if ship_version is not None:
        # 只给版本号而无船号：同样视为混用/不完整输入
        raise _mixed_error("ship_version_without_ship_number")

    if present:
        missing = [name for name in PARAM_FIELDS if name not in present]
        if missing:
            raise _incomplete_params(missing)
    else:
        raise _incomplete_params(list(PARAM_FIELDS))
    return {"ship_number": None, "ship_version": None,
            "draft": draft, "height": height, "margin": margin}


def _load_current(conn, ship_number: str) -> dict:
    versions = list_versions(conn, ship_number)  # 未知船号 -> 404
    current = versions[-1]
    if not current["active"]:
        raise ApiError(
            409, "SHIP_INACTIVE",
            f"船舶 {ship_number} 当前已停用（版本 #{current['version']}），"
            "启用前不得预排",
            {"ship_number": ship_number, "current_version": current["version"]},
        )
    return {"ship_number": ship_number, "ship_version": current["version"],
            "draft": current["draft"], "height": current["height"],
            "margin": current["margin"]}


def _load_for_confirm(conn, ship_number: str, version: int) -> dict:
    """事务内核对档案仍启用且版本未变，返回回填参数与固化引用。"""
    detail = get_ship(conn, ship_number)  # 未知船号 -> 404 SHIP_NOT_FOUND
    requested = next(
        (v for v in detail["versions"] if v["version"] == version), None
    )
    if requested is None:
        raise _ship_version_not_found(ship_number, version)
    if version != detail["current_version"]:
        raise ApiError(
            409, "SHIP_VERSION_STALE",
            f"船舶 {ship_number} 档案已更新：预排所据版本 #{version} 已不是当前"
            f"版本 #{detail['current_version']}，请按当前版本重新预排",
            {"ship_number": ship_number,
             "provided_version": version,
             "current_version": detail["current_version"]},
        )
    if not requested["active"]:
        raise ApiError(
            409, "SHIP_INACTIVE",
            f"船舶 {ship_number} 所据版本 #{version} 当前已停用，确认失败",
            {"ship_number": ship_number, "current_version": version},
        )
    return {"ship_number": ship_number, "ship_version": version,
            "draft": requested["draft"], "height": requested["height"],
            "margin": requested["margin"]}
