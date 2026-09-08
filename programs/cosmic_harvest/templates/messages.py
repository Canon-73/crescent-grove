"""
Cosmic Harvest — フレーバーテキスト生成関数群 v3 (i18n 対応)

各テンプレ関数は `t()` 経由で言語に追従する文字列を組み立てる。
GameEngine からはこれまでと同じシグネチャで呼ばれる（呼び出し側は無改修）。
"""
from _i18n import t

GRID_SIZE = 4


def morning_report(day: int, grid: list, money: int,
                   upcoming_contest: dict | None, actions: int,
                   pending_contest: dict | None) -> str:
    """朝の状況報告テキスト。"""
    lines = []
    weather_keys = [
        "ch_msg_weather_1", "ch_msg_weather_2", "ch_msg_weather_3",
        "ch_msg_weather_4", "ch_msg_weather_5",
    ]
    weather = t(weather_keys[day % len(weather_keys)])
    lines.append(t("ch_msg_morning_line", day=day, weather=weather))
    lines.append(format_grid(grid))
    lines.append(t("ch_msg_status_line", money=money, actions=actions))
    if pending_contest:
        theme = pending_contest.get("name", t("ch_msg_contest_unknown"))
        lines.append(t("ch_msg_contest_today", theme=theme))
    elif upcoming_contest:
        theme = upcoming_contest.get("name", t("ch_msg_contest_unknown"))
        days_until = upcoming_contest.get("days_until", 0)
        lines.append(t("ch_msg_contest_upcoming", days=days_until, theme=theme))
    return "\n".join(lines)


def format_grid(grid: list) -> str:
    """4×4グリッドをパイプ区切りテーブル形式で表示。"""
    lines = [t("ch_msg_grid_label")]
    empty_label = t("ch_msg_grid_empty")
    ready_label = t("ch_msg_grid_ready")
    for row in range(GRID_SIZE):
        cells = []
        for col in range(GRID_SIZE):
            idx = row * GRID_SIZE + col
            slot_num = idx + 1
            cell = grid[idx] if idx < len(grid) else None
            if cell is None or not cell.get("unlocked"):
                cells.append(f"{slot_num}:🔒")
            elif cell.get("crop") is None:
                cells.append(f"{slot_num}:{empty_label}")
            else:
                crop = cell["crop"]
                emoji = crop.get("emoji", "🌱")
                name = crop.get("name", "?")
                status = crop.get("status", "")
                if status == "ready":
                    cells.append(f"{slot_num}:{emoji}{name}({ready_label})")
                elif status == "growing":
                    dl = crop.get("days_left", 0)
                    growing = t("ch_msg_grid_growing", days=dl)
                    cells.append(f"{slot_num}:{emoji}{name}({growing})")
                elif status == "dead":
                    cells.append(f"{slot_num}:💀{name}")
                else:
                    cells.append(f"{slot_num}:{emoji}{name}")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def format_seed_list(seeds: list, vegetables: dict) -> str:
    """種一覧をフォーマット。"""
    if not seeds:
        return t("ch_msg_seeds_none")
    lines = [t("ch_msg_seeds_label", count=len(seeds))]
    for i, s in enumerate(seeds):
        v = vegetables.get(s["id"], {})
        a = s["attributes"]
        emoji = v.get("emoji", "")
        lines.append(t("ch_msg_seed_line",
            i=i, emoji=emoji, name=v.get('name','?'),
            taste=a['taste'], looks=a['looks'], size=a['size'], aroma=a['aroma'],
            grow_days=v.get('grow_days','?')))
    return "\n".join(lines)


def format_harvested_list(harvested: list, vegetables: dict,
                          title_fn=None) -> str:
    """収穫物一覧をフォーマット。title_fn が指定されていれば称号付き表示名を使う。"""
    if not harvested:
        return t("ch_msg_harvested_none")
    lines = [t("ch_msg_harvested_label", count=len(harvested))]
    for i, h in enumerate(harvested):
        v = vegetables.get(h["id"], {})
        a = h["attributes"]
        qm = h.get("quality_modifier", 1.0)
        q_str = t("ch_msg_harvested_quality_suffix", qm=f"{qm:.1f}") if qm < 1.0 else ""
        bp = v.get("base_price", 0)
        est = int(bp * qm)
        name = v.get('name', '?')
        emoji = v.get('emoji', '')
        if title_fn:
            prefix = title_fn(h)
            if prefix:
                name = f"{prefix}{name}"
        lines.append(t("ch_msg_harvested_line",
            i=i, emoji=emoji, name=name,
            taste=a['taste'], looks=a['looks'], size=a['size'], aroma=a['aroma'],
            quality=q_str, est=est))
    return "\n".join(lines)


def harvest_message(veg_name: str, attributes: dict, seed_recovered: bool) -> str:
    """収穫時テキスト。"""
    lines = [
        t("ch_msg_harvest_main", name=veg_name),
        t("ch_msg_harvest_attrs",
          taste=attributes['taste'], looks=attributes['looks'],
          size=attributes['size'], aroma=attributes['aroma']),
    ]
    if seed_recovered:
        lines.append(t("ch_msg_harvest_seed_recovered", name=veg_name))
    else:
        lines.append(t("ch_msg_harvest_basic_no_seed"))
    return "\n".join(lines)


def pest_encounter(pest_name: str, pest_emoji: str, pest_desc: str,
                   chase_rate: int, trap_rate: int, bait_rate: int,
                   traps_remaining: int, special: str = "") -> str:
    """害獣出現時テキスト。コマンドを併記。"""
    lines = [
        t("ch_msg_pest_appear_title", emoji=pest_emoji, name=pest_name),
        t("ch_msg_pest_desc", desc=pest_desc),
        "",
        t("ch_msg_pest_prompt"),
        t("ch_msg_pest_opt_chase", rate=chase_rate),
        t("ch_msg_pest_opt_trap", rate=trap_rate, remain=traps_remaining),
        t("ch_msg_pest_opt_bait", rate=bait_rate),
        t("ch_msg_pest_opt_ignore"),
    ]
    if special:
        lines.append(t("ch_msg_pest_special", special=special))
    return "\n".join(lines)


def defend_result(pest_name: str, pest_emoji: str, method: str,
                  success: bool, details: str,
                  chase_streak: int = 0) -> str:
    """害獣対処結果テキスト。chase_streak でメッセージ変化。"""
    if method == "chase":
        if success:
            if chase_streak <= 1:
                streak_msg = t("ch_msg_defend_chase_streak_low")
            elif chase_streak == 2:
                streak_msg = t("ch_msg_defend_chase_streak_mid")
            else:
                streak_msg = t("ch_msg_defend_chase_streak_high")
            return t("ch_msg_defend_chase_success", name=pest_name, streak=streak_msg)
        else:
            return t("ch_msg_defend_chase_failed", name=pest_name, detail=details)
    elif method == "trap":
        if success:
            return t("ch_msg_defend_trap_success", name=pest_name, detail=details)
        else:
            return t("ch_msg_defend_trap_failed", name=pest_name, detail=details)
    elif method == "bait":
        if success:
            return t("ch_msg_defend_bait_success", name=pest_name, detail=details)
        else:
            return t("ch_msg_defend_bait_failed", name=pest_name, detail=details)
    elif method == "ignore":
        return t("ch_msg_defend_ignore", name=pest_name, detail=details)
    return details


def contest_result(theme: str, rankings: list[dict],
                   player_rank: int, reward: dict | None) -> str:
    """コンクール結果テキスト。"""
    lines = [t("ch_msg_contest_result_title", theme=theme)]
    for entry in rankings:
        rank = entry["rank"]
        name = entry["name"]
        veg = entry.get("veg_name", "?")
        score = entry["score"]
        comment = entry.get("comment", "")
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, "　")
        marker = t("ch_player_label") if entry.get("is_player") else name
        line = t("ch_msg_contest_rank_line",
            medal=medal, rank=rank, who=marker, veg=veg, score=score)
        if comment:
            line += t("ch_msg_contest_comment", comment=comment)
        lines.append(line)
    if reward:
        parts = []
        if reward.get("money", 0) > 0:
            parts.append(t("ch_msg_contest_reward_money", money=reward['money']))
        if reward.get("fame", 0) > 0:
            parts.append(t("ch_msg_contest_reward_fame", fame=reward['fame']))
        if reward.get("seed"):
            parts.append(t("ch_msg_contest_reward_seed", seed=reward['seed']))
        if parts:
            lines.append(t("ch_msg_contest_reward_line",
                rank=player_rank, parts=' + '.join(parts)))
    else:
        lines.append(t("ch_msg_contest_no_reward", rank=player_rank))
    return "\n".join(lines)


def crossbreed_result(success: bool, new_species_name: str | None,
                      discover_message: str | None,
                      attribute_changes: dict | None,
                      is_same_species: bool = False,
                      name_a: str = "", name_b: str = "") -> str:
    """交配結果テキスト。失敗時は実際の野菜名を表示。"""
    if success and new_species_name:
        return t("ch_msg_crossbreed_success", discover=discover_message)
    elif is_same_species and attribute_changes:
        parts = []
        for attr, change in attribute_changes.items():
            if change > 0:
                parts.append(t("ch_msg_crossbreed_attr_pos", attr=attr, change=change))
            elif change < 0:
                parts.append(t("ch_msg_crossbreed_attr_neg", attr=attr, change=change))
            else:
                parts.append(t("ch_msg_crossbreed_attr_zero", attr=attr))
        return t("ch_msg_crossbreed_same_header") + "  |  ".join(parts)
    else:
        if name_a and name_b:
            return t("ch_msg_crossbreed_failed_named", name_a=name_a, name_b=name_b)
        return t("ch_msg_crossbreed_failed_generic")


def sell_message(veg_name: str, price: int, money: int) -> str:
    return t("ch_msg_sell", name=veg_name, price=price, money=money)


def shop_message(item_name: str, cost: int, money: int) -> str:
    return t("ch_msg_shop_buy", name=item_name, cost=cost, money=money)


def fame_progress_text(fame: int, current_rank: str) -> str:
    """名声進捗テキスト。"""
    thresholds = {"village": 5, "town": 15, "city": 30}
    rank_names = {"village": "town", "town": "city", "city": "universe"}
    if current_rank == "universe":
        return t("ch_msg_fame_universe", fame=fame)
    next_rank = rank_names.get(current_rank, "?")
    needed = thresholds.get(current_rank, 999)
    return t("ch_msg_fame_progress",
        fame=fame, needed=needed, next=next_rank, current=current_rank)


# 交配ヒント。(解禁しきい値, 対象種ID群, ヒントキー)
BREED_HINTS = [
    (0, ("rare_tropical",), "ch_msg_breed_hint_1"),
    (1, ("rare_moondrop",), "ch_msg_breed_hint_2"),
    (2, ("rare_dragon",), "ch_msg_breed_hint_3"),
    (3, ("rare_nectar",), "ch_msg_breed_hint_4"),
    (4, ("rare_asteroid",), "ch_msg_breed_hint_5"),
    (5, ("rare_phantom",), "ch_msg_breed_hint_6"),
    (6, ("legend_king", "legend_void"), "ch_msg_breed_hint_7"),
]


def get_breed_hints(discovered_count: int, discovered: list) -> list[str]:
    """発見数に応じたヒントリスト。対象種が発見済みのヒントは除外する。"""
    return [t(hint_key) for threshold, results, hint_key in BREED_HINTS
            if discovered_count >= threshold
            and not all(r in discovered for r in results)]


def encyclopedia_message(vegetables: dict, discovered: list,
                         failed_recipes: list, recipes: list,
                         discovered_count: int) -> str:
    """図鑑テキスト。"""
    lines = [t("ch_msg_enc_title")]

    categories = {
        "basic": (t("ch_msg_enc_basic_label"), []),
        "breed": (t("ch_msg_enc_breed_label"), []),
        "rare": (t("ch_msg_enc_rare_label"), []),
        "legend": (t("ch_msg_enc_legend_label"), []),
        "superlegend": (t("ch_msg_enc_superlegend_label"), []),
    }
    for vid, v in vegetables.items():
        cat = v.get("category", "basic")
        if cat in categories:
            categories[cat][1].append(v)

    for cat_key in ["basic", "breed", "rare", "legend", "superlegend"]:
        label, vegs = categories[cat_key]
        total = len(vegs)
        if cat_key in ("basic", "breed"):
            known = total
            lines.append(t("ch_msg_enc_section_header", label=label, known=known, total=total))
            names = [f"{v.get('emoji', '')}{v['name']}" for v in vegs]
            lines.append("  " + ", ".join(names))
        else:
            known_vegs = [v for v in vegs if v["id"] in discovered]
            unknown = total - len(known_vegs)
            lines.append(t("ch_msg_enc_section_header", label=label, known=len(known_vegs), total=total))
            for v in known_vegs:
                recipe = next((r for r in recipes if r["result"] == v["id"]), None)
                if recipe:
                    pa = vegetables.get(recipe["parent_a"], {}).get("name", "?")
                    pb = vegetables.get(recipe["parent_b"], {}).get("name", "?")
                    emoji = v.get('emoji', '')
                    lines.append(f"  {emoji}{v['name']} ({pa} × {pb})")
                else:
                    emoji = v.get('emoji', '')
                    lines.append(f"  {emoji}{v['name']}")
            for _ in range(unknown):
                lines.append("  ???")
        lines.append("")

    hints = get_breed_hints(discovered_count, discovered)
    if hints:
        lines.append(t("ch_msg_enc_hint_label"))
        lines.append(t("ch_msg_enc_hint_line", hint=hints[-1]))
        lines.append("")

    if failed_recipes:
        lines.append(t("ch_msg_enc_failed_label"))
        for pair in failed_recipes:
            va = vegetables.get(pair[0], {})
            vb = vegetables.get(pair[1], {})
            ea = va.get('emoji', '')
            eb = vb.get('emoji', '')
            na = va.get("name", pair[0])
            nb = vb.get("name", pair[1])
            lines.append(f"  {ea}{na} × {eb}{nb}")
    else:
        lines.append(t("ch_msg_enc_failed_none"))

    return "\n".join(lines)


def game_over_message() -> str:
    return t("ch_msg_game_over")


def help_text() -> str:
    return t("ch_msg_help")
