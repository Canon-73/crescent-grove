"""コマンドモジュール集約"""
from commands import setup_cmd, read_cmd, post_cmd, help_cmd

REGISTRY = {}


def _register(module):
    for name, func in getattr(module, "COMMANDS", {}).items():
        REGISTRY[name] = func


_register(setup_cmd)
_register(read_cmd)
_register(post_cmd)
_register(help_cmd)
