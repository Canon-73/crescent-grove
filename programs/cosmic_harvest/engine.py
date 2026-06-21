"""
Cosmic Harvest — ゲームエンジン本体 v3

v2からの変更:
  修正9:  chase失敗時に被害発生
  修正10: defend_result に chase_streak を渡す
  修正11: crossbreed_result に野菜名を渡す
  修正12: statusに名声進捗
  修正13: 引数バリデーション強化
  修正14: コンクール出品で野菜を消費しない
  修正15: 交配ヒント（図鑑内）
  修正16: 交配失敗済み組み合わせのブロック
  修正17: 図鑑コマンド追加
  修正7:  sell レスポンスに収穫物一覧
  修正8:  shop 品揃えに所持金
"""
import copy
import csv
import json
import os
import random
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import sys

from _i18n import t
sys.path.insert(0, str(Path(__file__).parent))
from templates.messages import (
    morning_report, harvest_message, pest_encounter, defend_result,
    contest_result, crossbreed_result, sell_message, shop_message,
    game_over_message, help_text, format_grid, format_seed_list,
    format_harvested_list, fame_progress_text, encyclopedia_message,
)

# --- 定数 ---
DATA_DIR = Path(__file__).parent / "data"
INITIAL_MONEY = 500
MAX_ACTIONS = 3
GRID_SIZE = 4
TOTAL_SLOTS = GRID_SIZE * GRID_SIZE
CONTEST_INTERVAL = 7
PEST_BASE_RATE = 0.3
PEST_RATE_PER_DAY = 0.005
PEST_RATE_MAX = 0.6
CHASE_STREAK_PENALTY = 0.30
INITIAL_UNLOCKED = {0, 1, 4, 5}

EXPAND_COSTS = {
    5: 100, 6: 200, 7: 300, 8: 400, 9: 500,
    10: 700, 11: 900, 12: 1100, 13: 1400,
    14: 1700, 15: 2000, 16: 2500,
}

SHOP_PRICES = {
    "basic_apple": 15, "basic_mikan": 18, "basic_banana": 20,
    "basic_strawberry": 10, "basic_blueberry": 22, "basic_pine": 25,
    "basic_cherry": 12, "basic_peach": 15,
    "breed_mango": 50, "breed_melon": 80, "breed_watermelon": 60,
    "breed_grape": 70, "breed_kiwi": 45, "breed_lemon": 55,
    "trap": 100,
}

# 称号属性の優先度（値が小さいほど優先）
TITLE_PRIORITY = {"size": 1, "taste": 2, "aroma": 3, "looks": 4}

RANK_THRESHOLDS = {"village": 0, "town": 5, "city": 15, "universe": 30}
RANK_ORDER = ["village", "town", "city", "universe"]
RANK_CAPS = {"village": 60, "town": 80, "city": 100, "universe": 120}
RARITY_BONUS = {"basic": 0, "breed": 5, "rare": 15, "legend": 30, "superlegend": 50}

NPCS = [
    {"name": "シャルル", "style": "highend",
     "comment_win": "ふん、当然の結果だな",
     "comment_lose": "…次は本気を出す"},
    {"name": "ゲンジ", "style": "random",
     "comment_win": "気合いの勝利だーーー！",
     "comment_lose": "くぅ～！次こそは気合いで勝つ！"},
    {"name": "フラニー", "style": "balanced",
     "comment_win": "えへへ、ありがとう♪",
     "comment_lose": "うぅ…もっと頑張るね"},
    {"name": "ボブ", "style": "breeder",
     "comment_win": "……交配の成果だ",
     "comment_lose": "……まだ品種改良が足りないか"},
    {"name": "アブドゥル", "style": "gobou",
     "comment_win": "ゴボウ玉の真価がようやく認められたか！",
     "comment_lose": "ゴボウ玉の真の味わいを理解できぬとは…"},
]

BREED_VARIATION = [
    (10, 15, 40), (1, 9, 20), (0, 0, 10), (-9, -1, 15), (-20, -10, 15),
]


class GameEngine:
    """Cosmic Harvest ゲームエンジン v3"""

    def __init__(self, workspace: str, seed: int | None = None):
        self.workspace = workspace
        # セーブは workspace/program_data/cosmic_harvest/ に保存する。
        # 旧バージョンは workspace/cosmic_harvest/ に保存していたため、
        # 読み込み時のみ旧パスにフォールバックして記録を引き継ぐ。
        self.save_dir = Path(workspace) / "program_data" / "cosmic_harvest"
        self.save_path = self.save_dir / "save.json"
        self.save_path_old = Path(workspace) / "cosmic_harvest" / "save.json"
        self.rng = random.Random(seed) if seed is not None else random.Random()
        self.state = None
        self.vegetables = {}
        self.recipes = []
        self.pests = {}
        self.contests = []
        self.titles = []   # titles.csv データ
        self.config = {}
        self.load_master_data()
        self._load_config()

    def _load_config(self):
        """data/config.json を読み込む。"""
        config_path = DATA_DIR / "config.json"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        else:
            # デフォルト値
            self.config = {
                "stamina_max": 30,
                "stamina_cost_per_action": 1,
                "stamina_regen_per_hour": 5,
                "stamina_free_commands": ["status", "help", "encyclopedia", "enc", "save", "load", "shop_list"],
            }

    def _csv_path(self, name: str) -> Path:
        """data/{lang}/{name}.csv を優先し、無ければ data/ja/{name}.csv にフォールバック、
        さらに無ければ旧 data/{name}.csv（言語分離前のレイアウト）に降りる。"""
        lang = os.environ.get("CG_LANG", "ja")
        candidates = [DATA_DIR / lang / name, DATA_DIR / "ja" / name, DATA_DIR / name]
        for p in candidates:
            if p.exists():
                return p
        # 最終フォールバックは旧レイアウト（存在しなければ open 側でエラー）
        return DATA_DIR / name

    def load_master_data(self):
        with open(self._csv_path("vegetables.csv"), "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                row["grow_days"] = int(row["grow_days"])
                row["base_price"] = int(row["base_price"])
                for attr in ["taste", "looks", "size", "aroma"]:
                    row[attr] = int(row[attr])
                self.vegetables[row["id"]] = row
        with open(self._csv_path("recipes.csv"), "r", encoding="utf-8") as f:
            self.recipes = list(csv.DictReader(f))
        with open(self._csv_path("pests.csv"), "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                for k in ["chase_success", "trap_success", "bait_success"]:
                    row[k] = int(row[k])
                row["quality_effect"] = float(row["quality_effect"])
                self.pests[row["id"]] = row
        with open(self._csv_path("contests.csv"), "r", encoding="utf-8") as f:
            self.contests = list(csv.DictReader(f))
        # titles.csv
        titles_path = self._csv_path("titles.csv")
        if titles_path.exists():
            with open(titles_path, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    row["min"] = int(row["min"])
                    row["max"] = int(row["max"])
                    row["priority"] = int(row["priority"])
                    self.titles.append(row)

    # --- グリッド操作 ---

    def _make_grid(self) -> list:
        return [{"unlocked": i in INITIAL_UNLOCKED, "slot": None} for i in range(TOTAL_SLOTS)]

    def _unlocked_count(self) -> int:
        return sum(1 for c in self.state["farm"]["grid"] if c["unlocked"])

    def _is_inner(self, idx: int) -> bool:
        grid = self.state["farm"]["grid"]
        if not grid[idx]["unlocked"]:
            return False
        row, col = divmod(idx, GRID_SIZE)
        if row == 0 or row == GRID_SIZE - 1 or col == 0 or col == GRID_SIZE - 1:
            return False
        neighbors = [
            (row - 1) * GRID_SIZE + col,
            (row + 1) * GRID_SIZE + col,
            row * GRID_SIZE + (col - 1),
            row * GRID_SIZE + (col + 1),
        ]
        return all(grid[n]["unlocked"] for n in neighbors)

    def _get_outer_slots(self) -> list[int]:
        return [i for i in range(TOTAL_SLOTS)
                if self.state["farm"]["grid"][i]["unlocked"] and not self._is_inner(i)]

    def _get_inner_slots(self) -> list[int]:
        return [i for i in range(TOTAL_SLOTS)
                if self.state["farm"]["grid"][i]["unlocked"] and self._is_inner(i)]

    def _grid_summary(self) -> list:
        result = []
        for cell in self.state["farm"]["grid"]:
            if not cell["unlocked"]:
                result.append({"unlocked": False})
            elif cell["slot"] is None:
                result.append({"unlocked": True, "crop": None})
            else:
                slot = cell["slot"]
                veg = self.vegetables.get(slot["seed_id"], {})
                days_left = self._days_until_ready(slot)
                emoji = veg.get("emoji", "🌱")
                result.append({
                    "unlocked": True,
                    "crop": {"emoji": emoji, "name": veg.get("name", "?"),
                             "status": slot["status"],
                             "days_left": max(0, days_left)},
                })
        return result

    # --- セーブ / ロード ---

    def new_game(self) -> dict:
        self.state = {
            "version": "3.0",
            "day": 1,
            "money": INITIAL_MONEY,
            "fame": 0,
            "actions_remaining": MAX_ACTIONS,
            "contest_rank": "village",
            "farm": {"grid": self._make_grid()},
            "inventory": {"seeds": [], "harvested": [], "traps": 0},
            "discovered_species": [],
            "failed_recipes": [],           # 修正16
            "pest_history": {},
            "contest_history": [],
            "contest_theme_queue": self._shuffle_contest_queue(),
            "pending_pest": None,
            "pending_contest": None,
            "ending_reached": False,
            "play_stamina": self.config["stamina_max"],
            "last_play_time": datetime.now().isoformat(),
            "flags": {},
        }
        for sid in ["basic_apple", "basic_strawberry", "basic_mikan"]:
            veg = self.vegetables[sid]
            self.state["inventory"]["seeds"].append({
                "id": sid,
                "attributes": {a: veg[a] for a in ["taste", "looks", "size", "aroma"]},
            })
        self.save()
        msg = morning_report(
            1, self._grid_summary(), INITIAL_MONEY, None, MAX_ACTIONS, None)
        seed_list = format_seed_list(self.state["inventory"]["seeds"], self.vegetables)
        return self._success(t("ch_new_game_welcome", morning=msg, seed_list=seed_list), {
            "day": 1, "money": INITIAL_MONEY, "actions_remaining": MAX_ACTIONS,
        })

    def save(self):
        os.makedirs(self.save_dir, exist_ok=True)
        self.state["rng_state"] = self.rng.getstate()
        fd, tmp = tempfile.mkstemp(dir=str(self.save_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2,
                          default=_json_default)
            if os.path.exists(self.save_path):
                os.remove(self.save_path)
            os.rename(tmp, str(self.save_path))
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def load(self) -> bool:
        # 新パス優先、無ければ旧パスにフォールバック
        path = self.save_path if self.save_path.exists() else self.save_path_old
        if not path.exists():
            return False
        with open(path, "r", encoding="utf-8") as f:
            self.state = json.load(f)
        if self.state.get("rng_state"):
            rs = self.state["rng_state"]
            self.rng.setstate((rs[0], tuple(rs[1]), rs[2] if len(rs) > 2 else None))
        # v2 → v3 マイグレーション
        if "failed_recipes" not in self.state:
            self.state["failed_recipes"] = []
        # v3 → v3.1 マイグレーション (スタミナ)
        if "play_stamina" not in self.state:
            self.state["play_stamina"] = self.config["stamina_max"]
        if "last_play_time" not in self.state:
            self.state["last_play_time"] = datetime.now().isoformat()
        return True

    # --- ディスパッチ ---

    def dispatch(self, command: str, args: dict) -> dict:
        # 修正13: 引数バリデーション
        if not isinstance(args, dict):
            args = {}

        COMMANDS = {
            "help": lambda a: self._success(help_text()),
            "new_game": lambda a: self.new_game(),
            "status": lambda a: self.cmd_status(),
            "plant": lambda a: self.cmd_plant(a),
            "harvest": lambda a: self.cmd_harvest(a),
            "crossbreed": lambda a: self.cmd_crossbreed(a),
            "shop": lambda a: self.cmd_shop(a),
            "sell": lambda a: self.cmd_sell(a),
            "contest": lambda a: self.cmd_contest(a),
            "defend": lambda a: self.cmd_defend(a),
            "next_day": lambda a: self.cmd_next_day(),
            "encyclopedia": lambda a: self.cmd_encyclopedia(),
            "enc": lambda a: self.cmd_encyclopedia(),
            "save": lambda a: self._cmd_save(),
            "load": lambda a: self._cmd_load(),
        }
        if command not in COMMANDS:
            return self._error(t("ch_dispatch_unknown_command", command=command))
        if command not in ("help", "new_game", "load") and self.state is None:
            return self._error(t("ch_dispatch_no_game"))

        # --- 修正18: スタミナ制 ---
        if self.state is not None:
            self._regen_stamina()
            # コマンドが無料かどうか判定
            free_cmds = self.config.get("stamina_free_commands", [])
            is_free = command in free_cmds
            # shop は引数なし（品揃え表示）の場合は shop_list 扱いで無料
            if command == "shop" and not args.get("item"):
                is_free = "shop_list" in free_cmds
            # new_game はスタミナチェック不要
            if command == "new_game":
                is_free = True

            if not is_free:
                cost = self.config.get("stamina_cost_per_action", 1)
                if self.state["play_stamina"] < cost:
                    return self._stamina_exhausted()
                self.state["play_stamina"] -= cost

        try:
            return COMMANDS[command](args)
        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)
            return self._error(t("ch_dispatch_internal_error", e=e))

    def _regen_stamina(self):
        """経過時間に基づきスタミナを回復する。"""
        last_str = self.state.get("last_play_time")
        if not last_str:
            self.state["last_play_time"] = datetime.now().isoformat()
            return
        try:
            last_time = datetime.fromisoformat(last_str)
        except (ValueError, TypeError):
            self.state["last_play_time"] = datetime.now().isoformat()
            return
        now = datetime.now()
        elapsed_hours = (now - last_time).total_seconds() / 3600
        regen_rate = self.config.get("stamina_regen_per_hour", 5)
        stamina_max = self.config.get("stamina_max", 30)
        regen = int(elapsed_hours * regen_rate)
        if regen > 0:
            self.state["play_stamina"] = min(
                stamina_max, self.state.get("play_stamina", 0) + regen)
        self.state["last_play_time"] = now.isoformat()

    def _stamina_exhausted(self) -> dict:
        """スタミナ切れメッセージ。"""
        stamina_max = self.config.get("stamina_max", 30)
        regen_rate = self.config.get("stamina_regen_per_hour", 5)
        # 1スタミナ回復するのに何分かかるか
        minutes_per_stamina = 60 / regen_rate if regen_rate > 0 else 999
        return self._error(t("ch_stamina_exhausted", stamina_max=stamina_max, minutes=int(minutes_per_stamina)))

    # --- アクション消費コマンド ---

    def cmd_plant(self, args: dict) -> dict:
        if self.state["pending_pest"]:
            return self._error(t("ch_pest_pending"))
        if self.state["actions_remaining"] <= 0:
            return self._error(t("ch_actions_exhausted_with_hint"))

        slot = args.get("slot")
        seed_index = args.get("seed_index")
        if slot is None or seed_index is None:
            return self._error(t("ch_plant_arg_invalid"))

        try:
            slot = int(slot)
            seed_index = int(seed_index)
        except (ValueError, TypeError):
            return self._error(t("ch_plant_arg_int"))

        idx = slot - 1
        if idx < 0 or idx >= TOTAL_SLOTS:
            return self._error(t("ch_slot_out_of_range_16", slot=slot))

        grid = self.state["farm"]["grid"]
        if not grid[idx]["unlocked"]:
            return self._error(t("ch_slot_locked_buy_hint", slot=slot))
        if grid[idx]["slot"] is not None:
            return self._error(t("ch_slot_occupied", slot=slot))

        seeds = self.state["inventory"]["seeds"]
        if seed_index < 0 or seed_index >= len(seeds):
            return self._error(t("ch_seed_index_out_of_range", seed_index=seed_index, count=len(seeds)))

        seed = seeds.pop(seed_index)
        veg = self.vegetables[seed["id"]]
        grid[idx]["slot"] = {
            "seed_id": seed["id"],
            "planted_day": self.state["day"],
            "attributes": copy.deepcopy(seed["attributes"]),
            "status": "growing",
            "quality_modifier": 1.0,
        }
        self.state["actions_remaining"] -= 1
        self.save()

        seed_list = format_seed_list(self.state["inventory"]["seeds"], self.vegetables)
        return self._success(
            t("ch_plant_success",
              name=veg['name'], slot=slot, grow_days=veg['grow_days'],
              actions=self.state['actions_remaining'], seed_list=seed_list),
            {"slot": slot, "seed": seed["id"],
             "actions_remaining": self.state["actions_remaining"],
             "current_seeds": self.state["inventory"]["seeds"]})

    def cmd_harvest(self, args: dict) -> dict:
        if self.state["pending_pest"]:
            return self._error(t("ch_pest_pending"))
        if self.state["actions_remaining"] <= 0:
            return self._error(t("ch_actions_exhausted_simple"))

        slot = args.get("slot")
        if slot is None:
            return self._error(t("ch_harvest_arg_invalid"))
        try:
            slot = int(slot)
        except (ValueError, TypeError):
            return self._error(t("ch_harvest_arg_int"))

        idx = slot - 1
        if idx < 0 or idx >= TOTAL_SLOTS:
            return self._error(t("ch_slot_out_of_range", slot=slot))

        grid = self.state["farm"]["grid"]
        if not grid[idx]["unlocked"]:
            return self._error(t("ch_slot_locked", slot=slot))
        crop = grid[idx]["slot"]
        if crop is None:
            return self._error(t("ch_slot_empty", slot=slot))
        if crop["status"] == "dead":
            grid[idx]["slot"] = None
            self.state["actions_remaining"] -= 1
            self.save()
            return self._success(t("ch_harvest_dead_cleared", actions=self.state['actions_remaining']))
        if crop["status"] != "ready":
            days_left = self._days_until_ready(crop)
            return self._error(t("ch_harvest_not_ready", days_left=days_left))

        veg = self.vegetables[crop["seed_id"]]
        harvested = {
            "id": crop["seed_id"],
            "attributes": copy.deepcopy(crop["attributes"]),
            "quality_modifier": crop["quality_modifier"],
        }
        self.state["inventory"]["harvested"].append(harvested)

        seed_recovered = veg["category"] != "basic"
        if seed_recovered:
            self.state["inventory"]["seeds"].append({
                "id": crop["seed_id"],
                "attributes": copy.deepcopy(crop["attributes"]),
            })

        grid[idx]["slot"] = None
        self.state["actions_remaining"] -= 1
        self.save()

        # 修正19: 称号付き表示名
        display_name = self.get_display_name(harvested)
        msg = harvest_message(display_name, crop["attributes"], seed_recovered)
        harv_list = format_harvested_list(
            self.state["inventory"]["harvested"], self.vegetables,
            title_fn=self.get_title_prefix)
        return self._success(
            msg + t("ch_harvest_actions_suffix",
                    actions=self.state['actions_remaining'], harv_list=harv_list),
            {"harvested": harvested, "seed_recovered": seed_recovered,
             "actions_remaining": self.state["actions_remaining"],
             "current_harvested": self.state["inventory"]["harvested"]})

    def cmd_crossbreed(self, args: dict) -> dict:
        if self.state["pending_pest"]:
            return self._error(t("ch_pest_pending"))
        if self.state["actions_remaining"] <= 0:
            return self._error(t("ch_actions_exhausted_simple"))

        idx_a = args.get("seed_index_a")
        idx_b = args.get("seed_index_b")
        if idx_a is None or idx_b is None:
            return self._error(t("ch_crossbreed_arg_invalid"))
        try:
            idx_a = int(idx_a)
            idx_b = int(idx_b)
        except (ValueError, TypeError):
            return self._error(t("ch_crossbreed_arg_int"))
        if idx_a == idx_b:
            return self._error(t("ch_crossbreed_same_seed"))

        seeds = self.state["inventory"]["seeds"]
        if idx_a < 0 or idx_a >= len(seeds) or idx_b < 0 or idx_b >= len(seeds):
            return self._error(t("ch_crossbreed_index_out_of_range", count=len(seeds)))

        seed_a = seeds[idx_a]
        seed_b = seeds[idx_b]
        veg_a = self.vegetables[seed_a["id"]]
        veg_b = self.vegetables[seed_b["id"]]

        if veg_a["category"] == "basic" or veg_b["category"] == "basic":
            return self._error(t("ch_crossbreed_basic_not_allowed"))

        # 修正16: 失敗済み組み合わせチェック（異種のみ）
        if seed_a["id"] != seed_b["id"]:
            pair = sorted([seed_a["id"], seed_b["id"]])
            if pair in self.state.get("failed_recipes", []):
                return self._error(t("ch_crossbreed_failed_pair", name_a=veg_a["name"], name_b=veg_b["name"]))

        for i in sorted([idx_a, idx_b], reverse=True):
            seeds.pop(i)
        self.state["actions_remaining"] -= 1

        if seed_a["id"] == seed_b["id"]:
            result = self._crossbreed_same(seed_a, seed_b)
        else:
            result = self._crossbreed_diff(seed_a, seed_b)

        seed_list = format_seed_list(self.state["inventory"]["seeds"], self.vegetables)
        result["message"] = result.get("message", "") + f"\n\n{seed_list}"
        if "data" in result:
            result["data"]["current_seeds"] = self.state["inventory"]["seeds"]
        self.save()
        return result

    # --- アクション非消費コマンド ---

    def cmd_shop(self, args: dict) -> dict:
        item = args.get("item")
        if not item:
            # 修正8: 品揃え表示の先頭に所持金
            lines = [t("ch_shop_money_label", money=self.state['money']), t("ch_shop_basic_label")]
            for sid, price in SHOP_PRICES.items():
                if sid.startswith("basic_") and sid in self.vegetables:
                    v = self.vegetables[sid]
                    emoji = v.get("emoji", "")
                    lines.append(f"  {sid:<18s} {emoji}{v['name']:<10s} {price}z  [{v.get('grow_days','?')}d]")
            lines.append(t("ch_shop_breed_label"))
            for sid, price in SHOP_PRICES.items():
                if sid.startswith("breed_") and sid in self.vegetables:
                    v = self.vegetables[sid]
                    emoji = v.get("emoji", "")
                    lines.append(f"  {sid:<18s} {emoji}{v['name']:<10s} {price}z  [{v.get('grow_days','?')}d]")
            lines.append(t("ch_shop_trap_label", price=SHOP_PRICES['trap']))
            unlocked = self._unlocked_count()
            if unlocked < TOTAL_SLOTS:
                next_cost = EXPAND_COSTS.get(unlocked + 1, 9999)
                lines.append(t("ch_shop_expand_label", cost=next_cost))
            lines.append(t("ch_shop_buy_hint"))
            return self._success("\n".join(lines), {"money": self.state["money"], "unlocked_slots": unlocked})

        if item == "expand_farm":
            slot_num = args.get("slot")
            if slot_num is None:
                return self._error(t("ch_shop_expand_arg_invalid"))
            try:
                slot_num = int(slot_num)
            except (ValueError, TypeError):
                return self._error(t("ch_shop_expand_arg_int"))
            idx = slot_num - 1
            if idx < 0 or idx >= TOTAL_SLOTS:
                return self._error(t("ch_slot_out_of_range_16", slot=slot_num))
            grid = self.state["farm"]["grid"]
            if grid[idx]["unlocked"]:
                return self._error(t("ch_shop_slot_already_unlocked", slot=slot_num))
            unlocked = self._unlocked_count()
            cost = EXPAND_COSTS.get(unlocked + 1, 9999)
            if self.state["money"] < cost:
                return self._error(t("ch_shop_not_enough_money_label", cost=cost, money=self.state['money']))
            self.state["money"] -= cost
            grid[idx]["unlocked"] = True
            self.save()
            grid_display = format_grid(self._grid_summary())
            return self._success(
                shop_message(t("ch_shop_expand_slot_name", slot=slot_num), cost, self.state["money"])
                + f"\n\n{grid_display}",
                {"slot": slot_num, "money": self.state["money"],
                 "unlocked_count": self._unlocked_count()})

        if item == "trap":
            if self.state["money"] < SHOP_PRICES["trap"]:
                return self._error(t("ch_shop_not_enough_money_short", cost=SHOP_PRICES['trap']))
            self.state["money"] -= SHOP_PRICES["trap"]
            self.state["inventory"]["traps"] += 1
            self.save()
            return self._success(
                shop_message(t("ch_shop_trap_name"), SHOP_PRICES["trap"], self.state["money"]),
                {"traps": self.state["inventory"]["traps"], "money": self.state["money"]})

        if item not in SHOP_PRICES or item not in self.vegetables:
            return self._error(t("ch_shop_unknown_item", item=item))

        price = SHOP_PRICES[item]
        if self.state["money"] < price:
            return self._error(t("ch_shop_not_enough_money_label", cost=price, money=self.state['money']))
        veg = self.vegetables[item]
        self.state["money"] -= price
        self.state["inventory"]["seeds"].append({
            "id": item,
            "attributes": {a: veg[a] for a in ["taste", "looks", "size", "aroma"]},
        })
        self.save()
        seed_list = format_seed_list(self.state["inventory"]["seeds"], self.vegetables)
        return self._success(
            shop_message(veg["name"], price, self.state["money"]) + f"\n\n{seed_list}",
            {"seed": item, "money": self.state["money"],
             "current_seeds": self.state["inventory"]["seeds"]})

    def cmd_sell(self, args: dict) -> dict:
        idx = args.get("harvest_index")
        if idx is None:
            return self._error(t("ch_sell_arg_invalid"))
        try:
            idx = int(idx)
        except (ValueError, TypeError):
            return self._error(t("ch_sell_arg_int"))
        harvested = self.state["inventory"]["harvested"]
        if idx < 0 or idx >= len(harvested):
            return self._error(t("ch_sell_index_out_of_range", idx=idx, count=len(harvested)))
        item = harvested.pop(idx)
        veg = self.vegetables[item["id"]]
        price = int(veg["base_price"] * item.get("quality_modifier", 1.0))
        self.state["money"] += price
        self.save()
        # 修正7: 残り収穫物一覧
        # 修正19: 称号付き表示名
        display_name = self.get_display_name(item)
        harv_list = format_harvested_list(
            self.state["inventory"]["harvested"], self.vegetables,
            title_fn=self.get_title_prefix)
        return self._success(
            sell_message(display_name, price, self.state["money"]) + f"\n\n{harv_list}",
            {"sold": item["id"], "price": price, "money": self.state["money"],
             "current_harvested": self.state["inventory"]["harvested"]})

    # --- イベントコマンド ---

    def cmd_contest(self, args: dict) -> dict:
        pc = self.state.get("pending_contest")
        if not pc:
            return self._error(t("ch_contest_not_open"))
        idx = args.get("harvest_index")
        if idx is None:
            return self._error(t("ch_contest_arg_invalid"))
        try:
            idx = int(idx)
        except (ValueError, TypeError):
            return self._error(t("ch_contest_arg_int"))
        harvested = self.state["inventory"]["harvested"]
        if idx < 0 or idx >= len(harvested):
            return self._error(t("ch_contest_index_out_of_range", idx=idx))

        # 修正14: 野菜を消費しない（pop → 参照のみ）
        entry = harvested[idx]
        veg = self.vegetables[entry["id"]]
        theme_data = pc["theme_data"]
        rank = self.state["contest_rank"]

        player_score = self._calc_score(entry, theme_data)

        # 修正5(v2): 不利な出品への警告
        warning = ""
        stat = theme_data["primary_stat"]
        if stat == "rarity" and veg["category"] == "basic":
            warning = t("ch_contest_warning_basic")
        elif stat in ("taste", "looks", "size", "aroma"):
            val = entry["attributes"].get(stat, 0)
            if val < 20:
                stat_name = t(f"ch_stat_{stat}")
                warning = t("ch_contest_warning_low_stat", stat_name=stat_name)

        npc_entries = self._gen_npc_entries(theme_data, rank)
        all_entries = npc_entries + [{
            "name": t("ch_player_label"), "veg_name": veg["name"],
            "score": player_score, "is_player": True,
        }]
        all_entries.sort(key=lambda x: x["score"], reverse=True)
        for i, e in enumerate(all_entries):
            e["rank"] = i + 1
            if not e.get("is_player"):
                npc_data = next((n for n in NPCS if n["name"] == e["name"]), None)
                if npc_data:
                    e["comment"] = npc_data["comment_win"] if e["rank"] == 1 else npc_data["comment_lose"]

        player_rank = next(e["rank"] for e in all_entries if e.get("is_player"))

        reward = None
        if player_rank == 1:
            reward = {"money": 300, "fame": 3}
            self.state["money"] += 300
            self.state["fame"] += 3
            rare_seeds = [v for v in self.vegetables.values()
                         if v["category"] == "rare" and v["id"] not in self.state["discovered_species"]]
            if rare_seeds:
                bonus_veg = self.rng.choice(rare_seeds)
                reward["seed"] = bonus_veg["name"]
                self.state["inventory"]["seeds"].append({
                    "id": bonus_veg["id"],
                    "attributes": {a: bonus_veg[a] for a in ["taste", "looks", "size", "aroma"]},
                })
                if bonus_veg["id"] not in self.state["discovered_species"]:
                    self.state["discovered_species"].append(bonus_veg["id"])
        elif player_rank == 2:
            reward = {"money": 150, "fame": 2}
            self.state["money"] += 150
            self.state["fame"] += 2
        elif player_rank == 3:
            reward = {"money": 50, "fame": 1}
            self.state["money"] += 50
            self.state["fame"] += 1


        self._check_rank_up()
        ending = False
        if rank == "universe" and player_rank == 1:
            self.state["ending_reached"] = True
            ending = True

        self.state["contest_history"].append({
            "day": self.state["day"], "theme": theme_data["name"], "rank": player_rank,
        })
        self.state["pending_contest"] = None
        self.save()

        msg = warning + contest_result(theme_data["name"], all_entries, player_rank, reward)
        if ending:
            msg += "\n\n" + game_over_message()
        return self._success(msg, {
            "theme": theme_data["name"], "player_rank": player_rank,
            "reward": reward, "ending": ending,
        })

    def cmd_defend(self, args: dict) -> dict:
        pp = self.state.get("pending_pest")
        if not pp:
            return self._error(t("ch_defend_no_pest"))
        method = args.get("method")
        if method not in ("chase", "trap", "bait", "ignore"):
            return self._error(t("ch_defend_arg_invalid"))

        pest = self.pests[pp["pest_id"]]
        pest_name = pest["name"]
        pest_emoji = pest["emoji"]
        result_details = ""
        success = False
        chase_streak = 0

        if method == "chase":
            roll = self.rng.randint(1, 100)
            success = roll <= pest["chase_success"]
            hist = self.state["pest_history"].setdefault(
                pp["pest_id"], {"encounters": 0, "caught": 0, "chase_streak": 0})
            if success:
                hist["chase_streak"] += 1
                chase_streak = hist["chase_streak"]
            else:
                # 修正9: chase失敗時に被害発生
                result_details = t("ch_defend_field_damaged", detail=self._apply_pest_damage(pp['pest_id']))

        elif method == "trap":
            if self.state["inventory"]["traps"] <= 0:
                return self._error(t("ch_defend_no_traps"))
            self.state["inventory"]["traps"] -= 1
            roll = self.rng.randint(1, 100)
            success = roll <= pest["trap_success"]
            if success:
                bounty = {"nebula_rabbit": 50, "crater_bear": 200,
                          "comet_fox": 300, "dust_mouse": 30, "void_moth": 150}
                reward = bounty.get(pp["pest_id"], 50)
                self.state["money"] += reward
                hist = self.state["pest_history"].setdefault(
                    pp["pest_id"], {"encounters": 0, "caught": 0, "chase_streak": 0})
                hist["caught"] += 1
                hist["chase_streak"] = 0
                result_details = t("ch_defend_trap_caught_reward", reward=reward)
            else:
                # 罠失敗: 被害なし、罠消費のみ
                result_details = t("ch_defend_trap_broken", pest_name=pest_name)

        elif method == "bait":
            bait_slot = args.get("bait_slot")
            if bait_slot is None:
                return self._error(t("ch_defend_bait_slot_required"))
            try:
                bait_slot = int(bait_slot)
            except (ValueError, TypeError):
                return self._error(t("ch_defend_bait_slot_int"))
            bait_idx = bait_slot - 1
            if bait_idx < 0 or bait_idx >= TOTAL_SLOTS:
                return self._error(t("ch_defend_bait_slot_out_of_range", slot=bait_slot))
            grid = self.state["farm"]["grid"]
            if not grid[bait_idx]["unlocked"] or grid[bait_idx]["slot"] is None:
                return self._error(t("ch_slot_empty", slot=bait_slot))
            veg_name = self.vegetables[grid[bait_idx]["slot"]["seed_id"]]["name"]
            grid[bait_idx]["slot"] = None
            roll = self.rng.randint(1, 100)
            success = roll <= pest["bait_success"]
            if success:
                hist = self.state["pest_history"].setdefault(
                    pp["pest_id"], {"encounters": 0, "caught": 0, "chase_streak": 0})
                hist["chase_streak"] = 0
                result_details = t("ch_defend_bait_success", veg_name=veg_name)
            else:
                # 餌失敗: 被害なし、野菜消費のみ
                result_details = t("ch_defend_bait_failed", veg_name=veg_name)

        elif method == "ignore":
            result_details = self._apply_pest_damage(pp["pest_id"])

        self.state["pending_pest"] = None
        self.save()
        # 修正10: chase_streak を渡す
        msg = defend_result(pest_name, pest_emoji, method, success, result_details,
                            chase_streak=chase_streak)
        return self._success(msg, {
            "pest": pp["pest_id"], "method": method, "success": success,
        })

    # --- 修正17: 図鑑 ---

    def cmd_encyclopedia(self) -> dict:
        discovered = self.state.get("discovered_species", [])
        failed = self.state.get("failed_recipes", [])
        # レア種のみカウント
        rare_count = sum(1 for d in discovered
                        if self.vegetables.get(d, {}).get("category") in ("rare", "legend", "superlegend"))
        msg = encyclopedia_message(
            self.vegetables, discovered, failed, self.recipes, rare_count)
        return self._success(msg)

    # --- フロー制御 ---

    def cmd_next_day(self) -> dict:
        if self.state["pending_pest"]:
            return self._error(t("ch_pest_pending"))
        if self.state["pending_contest"]:
            self.state["contest_history"].append({
                "day": self.state["day"],
                "theme": self.state["pending_contest"]["theme_data"]["name"],
                "rank": -1,
            })
            self.state["pending_contest"] = None

        self.state["day"] += 1
        self.state["actions_remaining"] = MAX_ACTIONS
        self._grow_crops()
        self._check_contest()
        pest_info = self._check_pest()
        self.save()

        if pest_info:
            pest = self.pests[pest_info["pest_id"]]
            msg = pest_encounter(
                pest["name"], pest["emoji"], pest["description"],
                pest["chase_success"], pest["trap_success"], pest["bait_success"],
                self.state["inventory"]["traps"], pest.get("special", ""))
            # コンクール開催日なら害獣メッセージにコンクール情報を追加
            pc = self.state.get("pending_contest")
            if pc:
                contest_name = pc["theme_data"]["name"]
                msg = t("ch_next_day_pest_contest_today", name=contest_name, msg=msg)
            return {
                "status": "event", "event_type": "pest_attack", "message": msg,
                "data": {
                    "pest": pest_info["pest_id"],
                    "options": ["chase", "trap", "bait", "ignore"],
                    "chase_success_rate": pest["chase_success"],
                    "trap_success_rate": pest["trap_success"],
                    "bait_success_rate": pest["bait_success"],
                    "traps_remaining": self.state["inventory"]["traps"],
                    "contest_today": pc["theme_data"]["name"] if pc else None,
                },
            }

        upcoming = self._upcoming_contest()
        pc = self.state.get("pending_contest")
        pc_name = None
        if pc:
            pc_name = pc["theme_data"]["name"]
        msg = morning_report(
            self.state["day"], self._grid_summary(), self.state["money"],
            upcoming, self.state["actions_remaining"],
            {"name": pc_name} if pc_name else None)
        return self._success(msg, {
            "day": self.state["day"], "actions_remaining": MAX_ACTIONS,
            "money": self.state["money"], "upcoming_contest": upcoming,
        })
        upcoming = self._upcoming_contest()
        pc = self.state.get("pending_contest")
        pc_name = None
        if pc:
            pc_name = pc["theme_data"]["name"]
        msg = morning_report(
            self.state["day"], self._grid_summary(), self.state["money"],
            upcoming, self.state["actions_remaining"],
            {"name": pc_name} if pc_name else None)
        return self._success(msg, {
            "day": self.state["day"], "actions_remaining": MAX_ACTIONS,
            "money": self.state["money"], "upcoming_contest": upcoming,
        })

    def cmd_status(self) -> dict:
        upcoming = self._upcoming_contest()
        grid_display = format_grid(self._grid_summary())
        seeds = self.state["inventory"]["seeds"]
        harvested = self.state["inventory"]["harvested"]

        # 修正12: 名声進捗表示
        fame_text = fame_progress_text(self.state["fame"], self.state["contest_rank"])

        stamina = self.state.get("play_stamina", 0)
        stamina_max = self.config.get("stamina_max", 30)
        msg_lines = [
            t("ch_status_header_line",
              day=self.state['day'], money=self.state['money'],
              actions=self.state['actions_remaining'], max_actions=MAX_ACTIONS,
              stamina=stamina, stamina_max=stamina_max),
            fame_text,
            "",
            grid_display,
            "",
            format_seed_list(seeds, self.vegetables),
            "",
            format_harvested_list(harvested, self.vegetables),
            t("ch_status_trap_line", count=self.state['inventory']['traps']),
        ]

        if self.state.get("pending_contest"):
            cn = self.state["pending_contest"]["theme_data"]["name"]
            msg_lines.append(t("ch_status_contest_open", name=cn))
        elif upcoming:
            msg_lines.append(t("ch_status_contest_upcoming", name=upcoming['name'], days=upcoming['days_until']))

        if self.state.get("discovered_species"):
            names = [self.vegetables.get(s, {}).get("name", s) for s in self.state["discovered_species"]]
            msg_lines.append(t("ch_status_discovered_line", names=', '.join(names)))

        return self._success("\n".join(msg_lines), {
            "day": self.state["day"], "money": self.state["money"],
            "actions_remaining": self.state["actions_remaining"],
            "fame": self.state["fame"], "contest_rank": self.state["contest_rank"],
        })

    def _cmd_save(self) -> dict:
        self.save()
        return self._success(t("ch_save_done"))

    def _cmd_load(self) -> dict:
        if self.load():
            return self._success(t("ch_load_done", day=self.state['day']))
        return self._error(t("ch_load_no_save"))

    # --- 称号システム (修正19) ---

    def get_title_prefix(self, item: dict) -> str:
        """果物の最も突出した属性に応じた称号prefixを返す。"""
        attrs = item.get("attributes", {})
        if not attrs:
            return ""
        # 各属性の絶対値を計算し、最大のものを選出
        attr_abs = {a: abs(attrs.get(a, 0)) for a in TITLE_PRIORITY}
        max_abs = max(attr_abs.values())
        # 全属性が0〜24の範囲（全て「普通の」に該当）の場合は空文字
        if max_abs < 25:
            return ""
        # 同値時はpriority値が小さい属性を優先
        best_attr = min(
            (a for a, v in attr_abs.items() if v == max_abs),
            key=lambda a: TITLE_PRIORITY[a])
        val = attrs.get(best_attr, 0)
        # titles.csv から該当するprefixを探す
        for row in self.titles:
            if row["attribute"] == best_attr and row["min"] <= val <= row["max"]:
                return row["prefix"]
        return ""

    def get_display_name(self, item: dict) -> str:
        """称号prefix + 果物名を結合した表示名を返す。"""
        veg = self.vegetables.get(item.get("id", ""), {})
        name = veg.get("name", "?")
        prefix = self.get_title_prefix(item)
        if prefix:
            return f"{prefix}{name}"
        return name

    # --- 内部ロジック ---

    def _grow_crops(self):
        for cell in self.state["farm"]["grid"]:
            if not cell["unlocked"] or cell["slot"] is None:
                continue
            slot = cell["slot"]
            if slot["status"] != "growing":
                continue
            veg = self.vegetables.get(slot["seed_id"])
            if veg and self.state["day"] - slot["planted_day"] >= veg["grow_days"]:
                slot["status"] = "ready"

    def _check_pest(self) -> dict | None:
        day = self.state["day"]
        base_rate = min(PEST_BASE_RATE + day * PEST_RATE_PER_DAY, PEST_RATE_MAX)
        has_crops = any(
            c["unlocked"] and c["slot"] is not None and c["slot"]["status"] != "dead"
            for c in self.state["farm"]["grid"])
        if not has_crops:
            return None
        if self.rng.random() > base_rate:
            return None
        pest_weights = self._calc_pest_weights()
        pest_id = self._weighted_choice(pest_weights)
        if not pest_id:
            return None
        hist = self.state["pest_history"].setdefault(
            pest_id, {"encounters": 0, "caught": 0, "chase_streak": 0})
        hist["encounters"] += 1
        self.state["pending_pest"] = {"pest_id": pest_id}
        return self.state["pending_pest"]

    def _calc_pest_weights(self) -> dict:
        day = self.state["day"]
        base = {
            "nebula_rabbit": 40, "dust_mouse": 30,
            "comet_fox": max(0, 20 - max(0, 10 - day)),
            "void_jelly": max(0, 15 - max(0, 15 - day)),
            "crater_bear": max(0, 10 - max(0, 20 - day)),
        }
        weights = {}
        for pid, w in base.items():
            if w <= 0:
                continue
            hist = self.state["pest_history"].get(pid, {})
            streak = hist.get("chase_streak", 0)
            w += int(w * streak * CHASE_STREAK_PENALTY)
            if pid == "nebula_rabbit" and self._has_crop_in_farm("basic_mikan"):
                w *= 2
            elif pid == "dust_mouse" and self._has_crop_in_farm("breed_lemon"):
                w //= 2
            weights[pid] = w
        return weights

    def _has_crop_in_farm(self, seed_id: str) -> bool:
        return any(
            c["unlocked"] and c["slot"] is not None
            and c["slot"]["seed_id"] == seed_id and c["slot"]["status"] != "dead"
            for c in self.state["farm"]["grid"])

    def _check_contest(self) -> dict | None:
        if self.state["day"] % CONTEST_INTERVAL != 0:
            return None
        queue = self.state.get("contest_theme_queue", [])
        if not queue:
            queue = self._shuffle_contest_queue()
            self.state["contest_theme_queue"] = queue
        if self.rng.random() < 0.2:
            theme_id = "reverse"
        else:
            theme_id = queue.pop(0)
            if theme_id == "reverse":
                if queue:
                    theme_id = queue.pop(0)
                else:
                    queue = self._shuffle_contest_queue()
                    self.state["contest_theme_queue"] = queue
                    theme_id = queue.pop(0)
        theme_data = next((c for c in self.contests if c["id"] == theme_id), self.contests[0])
        self.state["pending_contest"] = {"theme_data": theme_data}
        return self.state["pending_contest"]

    def _shuffle_contest_queue(self) -> list:
        normal = [c["id"] for c in self.contests if c["id"] != "reverse"]
        self.rng.shuffle(normal)
        return normal

    def _upcoming_contest(self) -> dict | None:
        day = self.state["day"]
        next_day = ((day // CONTEST_INTERVAL) + 1) * CONTEST_INTERVAL
        days_until = next_day - day
        if days_until > 3:
            return None
        queue = self.state.get("contest_theme_queue", [])
        if queue:
            td = next((c for c in self.contests if c["id"] == queue[0]), None)
            if td:
                return {"name": td["name"], "days_until": days_until}
        return {"name": "（未定）", "days_until": days_until}

    def _calc_score(self, entry: dict, theme_data: dict) -> int:
        attrs = entry["attributes"]
        qm = entry.get("quality_modifier", 1.0)
        veg = self.vegetables.get(entry["id"], {})
        rarity = RARITY_BONUS.get(veg.get("category", "basic"), 0)
        stat = theme_data["primary_stat"]
        if stat in ("taste", "looks", "size", "aroma"):
            base = attrs.get(stat, 0) * qm
        elif stat == "all":
            base = sum(attrs.values()) * qm / 4
        elif stat == "rarity":
            base = rarity * 3 + sum(attrs.values()) * qm * 0.1
        elif stat == "lowest":
            base = (100 - sum(attrs.values()) / 4) * qm
        else:
            base = sum(attrs.values()) * qm / 4
        return max(0, int(base + rarity + self.rng.randint(-5, 5)))

    def _gen_npc_entries(self, theme_data: dict, rank: str) -> list:
        cap = RANK_CAPS.get(rank, 60)
        day = self.state["day"]
        entries = []
        for npc in NPCS:
            score = self._gen_npc_score(npc, day, cap)
            if npc["style"] == "gobou":
                veg_name = "ゴボウ玉"
            else:
                veg_names = [v["name"] for v in self.vegetables.values()
                            if v["category"] in ("breed", "rare")]
                veg_name = self.rng.choice(veg_names) if veg_names else "不明"
            entries.append({"name": npc["name"], "veg_name": veg_name,
                           "score": score, "is_player": False})
        return entries

    def _gen_npc_score(self, npc: dict, day: int, cap: int) -> int:
        style = npc["style"]
        if style == "highend":
            score = min(50 + day * 1.5, cap)
        elif style == "random":
            score = min(30 + self.rng.randint(0, 50), cap)
        elif style == "balanced":
            score = min(40 + day * 1.0, cap)
        elif style == "breeder":
            score = min(20 + max(0, day - 15) * 2.5, cap)
        elif style == "gobou":
            score = min(10 + self.rng.randint(0, 15), cap)
        else:
            score = 30
        return int(score + self.rng.randint(-5, 5))

    def _check_rank_up(self):
        fame = self.state["fame"]
        for rank in reversed(RANK_ORDER):
            if fame >= RANK_THRESHOLDS[rank]:
                if self.state["contest_rank"] != rank:
                    self.state["contest_rank"] = rank
                break

    def _crossbreed_same(self, seed_a: dict, seed_b: dict) -> dict:
        new_attrs = {}
        changes = {}
        for attr in ["taste", "looks", "size", "aroma"]:
            avg = (seed_a["attributes"][attr] + seed_b["attributes"][attr]) // 2
            variation = self._breed_variation()
            new_val = max(-100, min(100, avg + variation))
            new_attrs[attr] = new_val
            changes[attr] = new_val - avg
        new_seed = {"id": seed_a["id"], "attributes": new_attrs}
        self.state["inventory"]["seeds"].append(new_seed)
        msg = crossbreed_result(False, None, None, changes, is_same_species=True)
        veg = self.vegetables[seed_a["id"]]
        return self._success(
            f"🧬 {veg['name']}同士の交配！\n{msg}\n"
            f"残りアクション: {self.state['actions_remaining']}",
            {"new_seed": new_seed, "attribute_changes": changes,
             "actions_remaining": self.state["actions_remaining"]})

    def _crossbreed_diff(self, seed_a: dict, seed_b: dict) -> dict:
        veg_a = self.vegetables[seed_a["id"]]
        veg_b = self.vegetables[seed_b["id"]]
        recipe = None
        for r in self.recipes:
            if {r["parent_a"], r["parent_b"]} == {seed_a["id"], seed_b["id"]}:
                recipe = r
                break
        if not recipe:
            # 修正16: 失敗を記録
            pair = sorted([seed_a["id"], seed_b["id"]])
            if pair not in self.state["failed_recipes"]:
                self.state["failed_recipes"].append(pair)
            self.state["inventory"]["seeds"].append(seed_a)
            # 修正11: 実際の野菜名を表示
            msg = crossbreed_result(False, None, None, None,
                                    name_a=veg_a["name"], name_b=veg_b["name"])
            return self._success(msg + f"\n残りアクション: {self.state['actions_remaining']}", {
                "success": False, "returned_seed": seed_a,
                "actions_remaining": self.state["actions_remaining"],
            })
        result_id = recipe["result"]
        result_veg = self.vegetables[result_id]
        new_attrs = {}
        for attr in ["taste", "looks", "size", "aroma"]:
            avg = (seed_a["attributes"][attr] + seed_b["attributes"][attr]) // 2
            new_attrs[attr] = min(100, avg + self.rng.randint(0, 20))
        new_seed = {"id": result_id, "attributes": new_attrs}
        self.state["inventory"]["seeds"].append(new_seed)
        if result_id not in self.state["discovered_species"]:
            self.state["discovered_species"].append(result_id)
        msg = crossbreed_result(True, result_veg["name"], recipe["discover_message"], None)
        return self._success(msg + f"\n残りアクション: {self.state['actions_remaining']}", {
            "success": True, "new_species": result_id, "new_seed": new_seed,
            "actions_remaining": self.state["actions_remaining"],
        })

    def _breed_variation(self) -> int:
        total = sum(w for _, _, w in BREED_VARIATION)
        roll = self.rng.randint(1, total)
        cumulative = 0
        for lo, hi, weight in BREED_VARIATION:
            cumulative += weight
            if roll <= cumulative:
                return self.rng.randint(lo, hi) if lo <= hi else self.rng.randint(hi, lo)
        return 0

    def _apply_pest_damage(self, pest_id: str) -> str:
        pest = self.pests[pest_id]
        dtype = pest["damage_type"]
        qe = pest["quality_effect"]
        grid = self.state["farm"]["grid"]

        all_occupied = []
        outer_occupied = []
        for i, cell in enumerate(grid):
            if cell["unlocked"] and cell["slot"] is not None and cell["slot"]["status"] != "dead":
                all_occupied.append(i)
                if not self._is_inner(i):
                    outer_occupied.append(i)

        if not all_occupied:
            return "（畑に作物がないため被害なし）"

        if pest_id == "nebula_rabbit":
            targets = outer_occupied if outer_occupied else all_occupied
            t = self.rng.choice(targets)
            grid[t]["slot"]["quality_modifier"] = max(0.1, grid[t]["slot"]["quality_modifier"] * qe)
            name = self.vegetables[grid[t]["slot"]["seed_id"]]["name"]
            return f"品質低下: {name}(スロット{t+1})"

        elif pest_id == "dust_mouse":
            targets = outer_occupied if outer_occupied else all_occupied
            names = []
            for t in targets:
                grid[t]["slot"]["quality_modifier"] = max(0.1, grid[t]["slot"]["quality_modifier"] * qe)
                names.append(self.vegetables[grid[t]["slot"]["seed_id"]]["name"])
            return f"品質低下: {', '.join(names)}"

        elif pest_id == "crater_bear":
            count = min(2, len(all_occupied))
            targets = self.rng.sample(all_occupied, count)
            names = []
            for t in targets:
                grid[t]["slot"]["status"] = "dead"
                names.append(self.vegetables[grid[t]["slot"]["seed_id"]]["name"])
            return f"💀 枯死: {', '.join(names)}"

        elif pest_id == "comet_fox":
            harvested = self.state["inventory"]["harvested"]
            if harvested:
                best_idx = max(range(len(harvested)),
                              key=lambda i: self.vegetables[harvested[i]["id"]]["base_price"])
                stolen = harvested.pop(best_idx)
                name = self.vegetables[stolen["id"]]["name"]
                return f"🦊 {name}が盗まれた！"
            else:
                t = self.rng.choice(all_occupied)
                name = self.vegetables[grid[t]["slot"]["seed_id"]]["name"]
                grid[t]["slot"] = None
                return f"🦊 畑の{name}(スロット{t+1})が盗まれた！"

        elif pest_id == "void_moth":
            t = self.rng.choice(all_occupied)
            old_taste = grid[t]["slot"]["attributes"]["taste"]
            grid[t]["slot"]["attributes"]["taste"] = 0
            name = self.vegetables[grid[t]["slot"]["seed_id"]]["name"]
            return f"🦋 {name}(スロット{t+1})の味が消えた…（味: {old_taste} → 0）"

        return "不明な被害"

    def _days_until_ready(self, slot: dict) -> int:
        veg = self.vegetables.get(slot["seed_id"])
        if not veg:
            return 0
        return max(0, veg["grow_days"] - (self.state["day"] - slot["planted_day"]))

    def _weighted_choice(self, weights: dict) -> str | None:
        if not weights:
            return None
        items = list(weights.items())
        total = sum(w for _, w in items)
        roll = self.rng.randint(1, total)
        cumulative = 0
        for item, w in items:
            cumulative += w
            if roll <= cumulative:
                return item
        return items[-1][0]

    def _success(self, message: str = "", data: dict = None) -> dict:
        r = {"status": "success"}
        if message:
            if self.state and self.state.get("actions_remaining", 1) <= 0:
                message += "\n\n⏭️ アクションを使い切りました。next_day で翌日に進もう！"
            r["message"] = message
        if data:
            r["data"] = data
        return r

    def _error(self, message: str) -> dict:
        return {"status": "error", "message": message}


def _json_default(obj):
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
