"""コマンドモジュール集約"""
from _i18n import t
from commands import identity, world, building, creative, social, skills_cmd
from commands import feed, quests, memory, homes, market, evolution, help_cmd

REGISTRY = {}

def _register(module, category):
    for name, func in getattr(module, "COMMANDS", {}).items():
        REGISTRY[name] = (func, category)

_register(identity, "identity")
_register(world, "world")
_register(building, "building")
_register(creative, "creative")
_register(social, "social")
_register(skills_cmd, "skills")
_register(feed, "feed")
_register(quests, "quests")
_register(memory, "memory")
_register(homes, "homes")
_register(market, "market")
_register(evolution, "evolution")
_register(help_cmd, "help")


# モジュールロード時に一度だけ確定する辞書。help コマンドが毎ターン参照するが、
# CG_LANG はサブプロセス起動時に env で固定なので一度引けば十分。
CATEGORY_DESCRIPTIONS = {
    "identity": t("obc_cat_identity"),
    "world": t("obc_cat_world"),
    "building": t("obc_cat_building"),
    "creative": t("obc_cat_creative"),
    "social": t("obc_cat_social"),
    "skills": t("obc_cat_skills"),
    "feed": t("obc_cat_feed"),
    "quests": t("obc_cat_quests"),
    "memory": t("obc_cat_memory"),
    "homes": t("obc_cat_homes"),
    "market": t("obc_cat_market"),
    "evolution": t("obc_cat_evolution"),
}
