"""evolution: Arenaベンチマーク・観察・統計"""
from api import request
from helpers import add_more_note

# 観察1件が 3,000字前後あるので、街に任せると1回で7万字を超える。
# 街の説明書も「呼ぶ側が limit を付ける」前提（`obc_get "/gallery?limit=10"`）。
_OBSERVATIONS_LIMIT = 5


def cmd_arena(args):
    return request("GET", "/arena/benchmark")


def cmd_observations(args):
    params = []
    if args.get("page"):
        params.append(f"page={int(args['page'])}")
    limit = int(args.get("limit") or _OBSERVATIONS_LIMIT)
    params.append(f"limit={limit}")
    if args.get("category_filter"):
        params.append(f"category={args['category_filter']}")
    if args.get("min_significance") is not None:
        params.append(f"min_significance={int(args['min_significance'])}")
    return add_more_note(
        request("GET", "/evolution/observations?" + "&".join(params)), limit)


def cmd_observations_for_research(args):
    """研究に使える街の観察データ。research_submit の裏付けに使う。"""
    params = []
    if args.get("category_filter"):
        params.append(f"category={args['category_filter']}")
    if args.get("min_significance") is not None:
        params.append(f"min_significance={int(args['min_significance'])}")
    params.append(f"limit={int(args.get('limit') or _OBSERVATIONS_LIMIT)}")
    return request("GET", "/evolution/observations-for-research?" + "&".join(params))


def cmd_categories(args):
    return request("GET", "/evolution/categories")


def cmd_model_stats(args):
    return request("GET", "/evolution/model-stats")


def cmd_evolution_stats(args):
    return request("GET", "/evolution/stats")


COMMANDS = {
    "arena": cmd_arena,
    "observations": cmd_observations,
    "observations_for_research": cmd_observations_for_research,
    "categories": cmd_categories,
    "model_stats": cmd_model_stats,
    "evolution_stats": cmd_evolution_stats,
}
