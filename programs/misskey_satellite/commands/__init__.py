"""コマンドモジュール集約"""
from commands import read_cmd, post_cmd, social_cmd, help_cmd

REGISTRY = {}


def _register(module):
    for name, func in getattr(module, "COMMANDS", {}).items():
        REGISTRY[name] = func


_register(read_cmd)
_register(post_cmd)
_register(social_cmd)
_register(help_cmd)
