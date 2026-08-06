"""跨入口的不变量：同一条业务规则若有多个入口，每个入口都得守。

审计反复发现的一类 bug：单条路径守得很严（带注释解释为什么），批量/另一个入口却整个绕过。
这里按「规则」而不是按「端点」组织测试，新增入口时照着补一条即可。
"""
import pytest

from app.config import settings


@pytest.fixture()
def fake_plugin(tmp_path, monkeypatch):
    d = tmp_path / "soroban-scraper-taobao"
    (d / ".state").mkdir(parents=True)
    (d / "plugin.toml").write_text(
        'id = "taobao"\nname = "淘宝订单"\nplatform = "taobao"\n'
        'python = ".venv/bin/python"\nentry = "-m taobao_scraper"\nstate_dir = ".state"\n',
        encoding="utf-8")
    monkeypatch.setattr(settings, "SCRAPER_DIR", str(tmp_path))
    return d


# --- 规则一：已导入且账本单仍在的暂存行，不许删 -------------------------------
# 删了会留下没人认领的账本单，它继续占着 (order_no, platform) 唯一号；爬虫重抓会新建暂存行，
# 用户再点导入就撞唯一约束、永远导不进来。

def test_single_delete_refuses_imported(client):
    s = client.post("/api/staging", json={"order_no": "INV-1"}).json()
    client.post(f"/api/staging/{s['id']}/import")
    assert client.delete(f"/api/staging/{s['id']}").status_code == 409


def test_batch_delete_skips_imported(client, fake_plugin):
    """按账号清空暂存（插件页按钮）必须与单条删除同语义：跳过已导入的，并回报跳过数。"""
    client.post("/api/plugins/taobao/account", params={"name": "invacct"})
    keep = client.post("/api/staging", json={"order_no": "INV-KEEP",
                                             "platform_account": "invacct"}).json()
    order = client.post(f"/api/staging/{keep['id']}/import").json()
    client.post("/api/staging", json={"order_no": "INV-DROP", "platform_account": "invacct"})

    r = client.delete("/api/plugins/taobao/account/staging", params={"account": "invacct"})
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == 1 and body["skipped"] == 1

    rows = client.get("/api/staging", params={"limit": 500}).json()["items"]
    ids = {x["id"] for x in rows}
    assert keep["id"] in ids, "已导入行不该被删"
    assert client.get(f"/api/orders/{order['id']}").status_code == 200


def test_batch_delete_removes_row_whose_order_was_deleted(client, fake_plugin):
    """账本单已软删 → 暂存行不再是任何人的镜像，可以删（与单条路径的 _linked_order 同口径）。"""
    client.post("/api/plugins/taobao/account", params={"name": "invacct2"})
    s = client.post("/api/staging", json={"order_no": "INV-GONE",
                                          "platform_account": "invacct2"}).json()
    o = client.post(f"/api/staging/{s['id']}/import").json()
    client.delete(f"/api/orders/{o['id']}")
    r = client.delete("/api/plugins/taobao/account/staging", params={"account": "invacct2"})
    assert r.json() == {"ok": True, "deleted": 1, "skipped": 0}


# --- 规则二：已导入行的「导入状态」只能由 /import /ignore /删除 推进 ------------

def test_ignore_endpoint_refuses_imported(client):
    s = client.post("/api/staging", json={"order_no": "INV-2"}).json()
    client.post(f"/api/staging/{s['id']}/import")
    assert client.post(f"/api/staging/{s['id']}/ignore").status_code == 409


def test_patch_cannot_flip_imported_to_ignored(client):
    """PATCH 曾能绕过 /ignore 的门闸，留下「已忽略却挂着活账本单」的发散行。"""
    s = client.post("/api/staging", json={"order_no": "INV-3"}).json()
    client.post(f"/api/staging/{s['id']}/import")
    row = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
               if x["id"] == s["id"])
    r = client.patch(f"/api/staging/{s['id']}",
                     json={"version": row["version"], "import_status": "已忽略"})
    assert r.status_code == 409
    again = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
                 if x["id"] == s["id"])
    assert again["import_status"] == "已导入"


def test_patch_may_resend_same_status(client):
    """整表单回传（前端常见写法）不该因为带了个没变的 status 就 409。"""
    s = client.post("/api/staging", json={"order_no": "INV-4"}).json()
    client.post(f"/api/staging/{s['id']}/import")
    row = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
               if x["id"] == s["id"])
    r = client.patch(f"/api/staging/{s['id']}",
                     json={"version": row["version"], "import_status": row["import_status"], "title": "改了"})
    assert r.status_code == 200 and r.json()["title"] == "改了"


# --- 规则三：标签值必须装得进它会被写入的每一个数据列 --------------------------
# TagOption.value 是 VARCHAR(128)，但 Order.platform 只有 VARCHAR(32)。
# 超长在 MySQL 抛 DataError（不是 IntegrityError → 逃过全局 409 → 500），SQLite 则静默存下，
# 之后迁到 MySQL 时整次 replace_data 被这条脏数据卡死。

def test_add_tag_rejects_value_too_long_for_target_column(client):
    r = client.post("/api/tags/platform", json={"value": "平" * 33})
    assert r.status_code == 422 and "32" in r.json()["detail"]


def test_add_tag_accepts_value_at_limit(client):
    assert client.post("/api/tags/platform", json={"value": "p" * 32}).status_code == 200


def test_rename_tag_rejects_value_too_long(client):
    client.post("/api/tags/platform", json={"value": "短名"})
    r = client.post("/api/tags/platform/rename", params={"old": "短名", "new": "长" * 33})
    assert r.status_code == 422


def test_set_tag_color_rejects_value_too_long(client):
    r = client.put("/api/tags/platform/color", params={"value": "x" * 33, "color": 1})
    assert r.status_code == 422


def test_recipient_allows_longer_values(client):
    """收货人写进 ShipmentOrder.recipient(128)，限长应按各字段自己的目标列，而不是一刀切。"""
    assert client.post("/api/tags/recipient", json={"value": "人" * 100}).status_code == 200
    assert client.post("/api/tags/recipient", json={"value": "人" * 129}).status_code == 422


def test_plugin_account_name_length_capped(client, fake_plugin):
    """账号名会写进 platform_account(64)。"""
    assert client.post("/api/plugins/taobao/account",
                       params={"name": "a" * 65}).status_code == 422
    assert client.post("/api/plugins/taobao/account",
                       params={"name": "a" * 64}).status_code == 200


def test_plugin_account_rename_length_capped(client, fake_plugin):
    client.post("/api/plugins/taobao/account", params={"name": "shortname"})
    r = client.post("/api/plugins/taobao/account/rename",
                    params={"old": "shortname", "new": "b" * 65})
    assert r.status_code == 422
