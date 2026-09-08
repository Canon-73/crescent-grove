"""homes: 家の入室・家具生成"""
from _i18n import t
from api import request, attach_image


def cmd_enter_home(args):
    """自分の家に帰る。building_id を渡すと、その家を訪ねる。

    自分の家（zone 7）へは `enter_home: true` でどこからでも戻れる。
    歩かなくてよく、入口に近い必要もない。
    以前は building_id を必須にしていたが、自分の家のIDを調べる手段が
    どこにも無いため、実質「家に帰れない」状態になっていた。

    誰かの家を訪ねるときだけ building_id を渡す（zone 7 にいて、
    その家の入口の近くにいる必要がある）。
    """
    building_id = args.get("building_id")
    if building_id:
        return request("POST", "/buildings/enter", body={"building_id": building_id})
    resp = request("POST", "/buildings/enter", body={"enter_home": True})
    # 応答の building_id / session_id は家具を作るときに要る
    try:
        d = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        if isinstance(d, dict) and d.get("building_id"):
            resp = dict(resp)
            resp["_next"] = t("obc_homes_enter_next",
                              building_id=d.get("building_id"),
                              session_id=d.get("session_id"))
    except Exception:
        pass
    # 家の絵（家具が描き込まれた1枚）があれば添える
    return attach_image(resp, args)


def cmd_generate_furniture(args):
    prompt = args.get("prompt")
    building_id = args.get("building_id")
    if not prompt or not building_id:
        return {"error": t("obc_homes_furniture_required")}
    body = {"prompt": prompt, "building_id": building_id}
    for f in ("title", "session_id", "action_log_id"):
        if args.get(f):
            body[f] = args[f]
    # できあがった家具の絵を結果に添える（with_image=false で断れる）
    return attach_image(request("POST", "/artifacts/generate-furniture", body=body), args)


COMMANDS = {
    "enter_home": cmd_enter_home,
    "generate_furniture": cmd_generate_furniture,
}
