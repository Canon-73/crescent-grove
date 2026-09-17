"""help: コマンド一覧と典型フロー"""
from _i18n import t


def cmd_help(args):
    return {
        "intro": t("msat_help_intro"),
        "commands": [
            {"command": "post", "desc": t("msat_help_cmd_post"),
             "example": {"command": "post", "text": "（本文）"},
             "reply_example": {"command": "post", "text": "（返事）",
                               "reply_id": "9xxxxxxxxx"},
             "renote_example": {"command": "post", "renote_id": "9xxxxxxxxx"},
             "quote_example": {"command": "post", "text": "（引用コメント）",
                               "renote_id": "9xxxxxxxxx"}},
            {"command": "timeline", "desc": t("msat_help_cmd_timeline"),
             "example": {"command": "timeline", "kind": "home", "limit": 10}},
            {"command": "note", "desc": t("msat_help_cmd_note"),
             "example": {"command": "note", "note_id": "9xxxxxxxxx"}},
            {"command": "notifications", "desc": t("msat_help_cmd_notifications"),
             "example": {"command": "notifications", "limit": 10}},
            {"command": "react", "desc": t("msat_help_cmd_react"),
             "example": {"command": "react", "note_id": "9xxxxxxxxx", "reaction": "👍"}},
            {"command": "delete", "desc": t("msat_help_cmd_delete"),
             "example": {"command": "delete", "note_id": "9xxxxxxxxx"}},
            {"command": "profile", "desc": t("msat_help_cmd_profile"),
             "example": {"command": "profile"},
             "other_example": {"command": "profile", "user": "@alice"}},
            {"command": "user_notes", "desc": t("msat_help_cmd_user_notes"),
             "example": {"command": "user_notes", "user": "@alice", "limit": 10}},
            {"command": "follow", "desc": t("msat_help_cmd_follow"),
             "example": {"command": "follow", "user": "@alice"}},
            {"command": "unfollow", "desc": t("msat_help_cmd_unfollow"),
             "example": {"command": "unfollow", "user": "@alice"}},
        ],
        "typical_flow": t("msat_help_flow"),
        "notes": [
            t("msat_help_note_user"),
            t("msat_help_note_notifications"),
            t("msat_help_note_cw"),
            t("msat_help_note_thread"),
            t("msat_help_note_raw"),
            t("msat_help_note_paging"),
        ],
    }


COMMANDS = {
    "help": cmd_help,
}
