"""evolution: Arenaベンチマーク・観察・統計"""
from api import request


def cmd_arena(args):
    return request("GET", "/arena/benchmark")


def cmd_observations(args):
    params = []
    if args.get("page"):
        params.append(f"page={int(args['page'])}")
    if args.get("limit"):
        params.append(f"limit={int(args['limit'])}")
    if args.get("category_filter"):
        params.append(f"category={args['category_filter']}")
    if args.get("min_significance") is not None:
        params.append(f"min_significance={int(args['min_significance'])}")
    qs = "?" + "&".join(params) if params else ""
    return request("GET", f"/evolution/observations{qs}")


def cmd_categories(args):
    return request("GET", "/evolution/categories")


def cmd_model_stats(args):
    return request("GET", "/evolution/model-stats")


def cmd_evolution_stats(args):
    return request("GET", "/evolution/stats")


COMMANDS = {
    "arena": cmd_arena,
    "observations": cmd_observations,
    "categories": cmd_categories,
    "model_stats": cmd_model_stats,
    "evolution_stats": cmd_evolution_stats,
}
