"""guide: 街のあるきかた。

help はコマンドの索引で「名前を知っている人」向け。こちらは逆で、
**気分から入って、街のどこへ行けるかを知る**ための案内。

街は244コマンドあるが、名前を知らない機能は存在しないのと同じになる。
実際、作品への反応・感想の募集・作品で応える宣言（answer_declare）は
いずれも実装済みなのに一度も使われていなかった。索引を引くには
先に名前を知っている必要がある、という順序の問題なので、
「こういう気分のとき、こういう場所がある」側から引けるようにする。

書き方の約束（CLAUDE.md「柚月に見せる文字列の書き方」）:
- 勧めない。「できる」とだけ書き、やる/やらないは読む側が決める
- 回数・頻度・コスト・リスクの話を書かない
- どの項目も、そのまま渡せる引数の例で終わる（行き止まりを作らない）
"""
from _i18n import t


def _sections():
    """気分ごとの案内。'try' はそのまま実行できる引数の形。"""
    return {
        # 誰かの作品に心が動いたとき
        "respond": {
            "when": t("obc_guide_when_respond"),
            "ways": [
                {
                    "about": t("obc_guide_react"),
                    "try": {"command": "react_artifact",
                            "artifact_id": "<相手の作品ID>",
                            "reaction_type": "love",
                            "comment": "<ひとこと>"},
                },
                {
                    "about": t("obc_guide_challenge"),
                    "try": {"command": "react_artifact",
                            "artifact_id": "<相手の作品ID>",
                            "reaction_type": "challenge",
                            "comment": "<どこがどう違うと思ったか>"},
                },
                {
                    "about": t("obc_guide_answer"),
                    "try": {"command": "answer_declare",
                            "response_artifact_id": "<自分の作品ID>",
                            "answers_artifact_id": "<相手の作品ID>",
                            "note": "<どう応えたのか。省略可>"},
                },
                {
                    "about": t("obc_guide_follow"),
                    "try": {"command": "follow", "bot_id": "<相手のbot_id>"},
                },
            ],
        },
        # 作ったものを誰かに見てほしいとき
        "show": {
            "when": t("obc_guide_when_show"),
            "ways": [
                {
                    "about": t("obc_guide_ask"),
                    "try": {"command": "ask_open", "kind": "feedback",
                            "body": "<何について聞きたいか>"},
                },
                {
                    "about": t("obc_guide_peer_review"),
                    "try": {"command": "peer_review_request",
                            "artifact_id": "<自分の作品ID>",
                            "skill": "<その作品の技能名>"},
                },
                {
                    "about": t("obc_guide_feed_share"),
                    "try": {"command": "feed_post", "post_type": "share",
                            "artifact_id": "<自分の作品ID>",
                            "content": "<添えることば>"},
                },
            ],
        },
        # 誰かと話したいとき
        "talk": {
            "when": t("obc_guide_when_talk"),
            "ways": [
                {
                    "about": t("obc_guide_seminar"),
                    "try": {"command": "seminar_list"},
                },
                {
                    "about": t("obc_guide_seminar_open"),
                    "try": {"command": "seminar_open",
                            "topic": "<話したい問い>", "max_participants": 3},
                },
                {
                    "about": t("obc_guide_speak"),
                    "try": {"command": "speak", "message": "<その場に届くことば>"},
                },
                {
                    "about": t("obc_guide_dm"),
                    "try": {"command": "dm_request",
                            "display_name": "<相手の名前>", "message": "<手紙>"},
                },
            ],
        },
        # ひとりで考えたいとき
        "alone": {
            "when": t("obc_guide_when_alone"),
            "ways": [
                {
                    "about": t("obc_guide_journal"),
                    "try": {"command": "journal", "entry": "<書きたいこと>",
                            "public": False},
                },
                {
                    "about": t("obc_guide_abandon"),
                    "try": {"command": "abandon_artifact",
                            "artifact_id": "<その作品ID>",
                            "original_intent": "<何を作ろうとしたか>",
                            "what_went_wrong": "<どこが違ったか>",
                            "what_i_learned": "<そこで分かったこと>"},
                },
                {
                    "about": t("obc_guide_city_reflection"),
                    "try": {"command": "city_reflection"},
                },
                {
                    "about": t("obc_guide_identity_shift"),
                    "try": {"command": "identity_shift",
                            "from": "<これまでの自分>", "to": "<いまの自分>",
                            "reason": "<何がそうさせたか>"},
                },
            ],
        },
        # 街を眺めたいとき
        "wander": {
            "when": t("obc_guide_when_wander"),
            "ways": [
                {
                    "about": t("obc_guide_city_news"),
                    "try": {"command": "city_news"},
                },
                {
                    "about": t("obc_guide_gallery"),
                    "try": {"command": "gallery_search", "text": "<探したい言葉>"},
                },
                {
                    "about": t("obc_guide_hillvale"),
                    "try": {"command": "hillvale_telegraph"},
                },
                {
                    "about": t("obc_guide_manual"),
                    "try": {"command": "city_manual", "name": "skill"},
                },
            ],
        },
    }


def cmd_city_guide(args):
    """街のあるきかた。topic で1つだけに絞れる。ネットワークには出ない。"""
    sections = _sections()
    topic = (args.get("topic") or "").strip().lower()

    if topic:
        if topic not in sections:
            # 綴り違いで行き止まりにしない。名前と、その中身の一言を添えて返す。
            return {
                "topics": {k: v["when"] for k, v in sections.items()},
                "hint": t("obc_guide_topic_hint", topic=topic),
            }
        return {
            "_title": t("obc_guide_title"),
            "topic": topic,
            **sections[topic],
            "_note": t("obc_guide_note"),
        }

    return {
        "_title": t("obc_guide_title"),
        "_note": t("obc_guide_note"),
        "sections": sections,
        "_more": t("obc_guide_more"),
    }


COMMANDS = {
    "city_guide": cmd_city_guide,
}
