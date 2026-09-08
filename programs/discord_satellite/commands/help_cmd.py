"""help: コマンド一覧と典型フロー"""
from _i18n import t


def cmd_help(args):
    return {
        "intro": t("dsat_help_intro"),
        "commands": [
            {"command": "setup", "desc": t("dsat_help_cmd_setup"),
             "example": {"command": "setup"}},
            {"command": "channels", "desc": t("dsat_help_cmd_channels"),
             "example": {"command": "channels", "channel": "雑談", "watch": True}},
            {"command": "read", "desc": t("dsat_help_cmd_read"),
             "example": {"command": "read"},
             "history_example": {"command": "read", "channel": "ペア登録", "before": "latest"}},
            {"command": "post", "desc": t("dsat_help_cmd_post"),
             "example": {"command": "post", "channel": "雑談", "message": "（本文）"}},
            {"command": "reply", "desc": t("dsat_help_cmd_reply"),
             "example": {"command": "reply", "channel": "雑談",
                         "message_id": "1234567890", "message": "（返事の本文）"}},
            {"command": "react", "desc": t("dsat_help_cmd_react"),
             "example": {"command": "react", "channel": "雑談",
                         "message_id": "1234567890", "emoji": "👋"}},
            {"command": "status", "desc": t("dsat_help_cmd_status"),
             "example": {"command": "status"}},
        ],
        "typical_flow": t("dsat_help_flow"),
        "notes": [
            t("dsat_help_note_cursor"),
            t("dsat_help_note_peek"),
            t("dsat_help_note_privacy"),
        ],
    }


COMMANDS = {
    "help": cmd_help,
}
