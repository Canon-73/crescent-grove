# core/scheduler.py
"""
タスクスケジューラモジュール

役割:
    指定された時刻にMDファイル（指示書）を読み込み、
    エージェントに自動実行させるバックグラウンドスケジューラ。
    Moonbeat（定期的な自発思考パルス）の制御もここで行う。

データ構造:
    workspace/schedule.json に以下の形式でスケジュールを保存する:
    [
      {
        "id": "uuid",
        "name": "タスク名",
        "schedule_type": "daily" | "once" | "interval",
        "time": "HH:MM",                    # daily の場合
        "datetime": "YYYY-MM-DD HH:MM",     # once の場合
        "interval_minutes": 60,              # interval の場合（実行間隔・分）
        "start_time": "HH:MM",              # interval の場合（実行開始時刻）
        "end_time": "HH:MM",                # interval の場合（実行終了時刻）
        "task_file": "tasks/xxx.md",
        "enabled": true,
        "last_run": "YYYY-MM-DD HH:MM" | null
      }
    ]

処理フロー:
    1. サーバー起動時に Scheduler.start() を呼ぶ
    2. asyncio タスクとして _loop() が毎分チェック
    3. 実行時刻に達したタスクがあれば _execute_task() を呼ぶ
    4. タスクファイル（MD）を読み込み、server.pyのコールバック経由でAgent.process_message()に渡す
    5. 実行結果をログに記録し、last_run を更新
    6. onceタスクは実行後に自動削除

Moonbeat（月動）:
    設定ファイル: data/moonbeat_config.json
    定期的にエージェントに自由時間を与え、自発的な思考・行動を促す。
    時間帯制限、スタミナ/エネルギーチェック、動的間隔調整、
    フラッシュバック（過去記憶の断片注入）を含む。
"""

from core.time_utils import tlog
import asyncio
import json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from core.paths import data_file, resolve_path, config_file
from core.config_loader import apply_prompt_placeholders
from typing import Optional, Callable, Awaitable

from memory.manager import MemoryManager


# 日本標準時（タスク実行時刻の判定に使用）
JST = timezone(timedelta(hours=9))


class Scheduler:
    """
    タスクスケジューラ。

    daily/once/interval の3種類のスケジュールタスクと、
    Moonbeat（自発思考パルス）を管理する。
    毎分の監視ループで実行タイミングを判定し、server.pyから設定された
    コールバック経由でエージェントにタスクを実行させる。
    """

    def __init__(self, schedule_file: str, memory: MemoryManager):
        """
        スケジューラを初期化する。

        Args:
            schedule_file: schedule.json の絶対パス（タスク定義の永続化先）
            memory: 記憶管理インスタンス（タスクファイルの読み込みに使用）
        """
        self.schedule_file = Path(schedule_file)
        self.memory = memory
        self.schedules: list[dict] = []
        self._task: Optional[asyncio.Task] = None

        # タスク実行時に呼ばれるコールバック（server.pyのexecute_scheduled_taskが設定される）
        self._execute_callback: Optional[Callable[[str, str, str], Awaitable[str]]] = None

        # --- Moonbeat関連の状態 ---
        self._last_moonbeat: Optional[datetime] = datetime.now(JST)  # 最終パルス時刻
        self._moonbeat_extension: float = 0  # 動的間隔の延長分（分）。トークン消費量に応じて増加
        self._prev_enabled: Optional[bool] = None  # 前回tickのenabled状態（OFF→ON遷移検知用）

        # server.pyのstartup_eventから設定される外部参照
        self.vital_manager = None  # VitalManagerインスタンス（スタミナ/エネルギーチェック用）
        self.rag_db = None         # RAGデータベースインスタンス（フラッシュバック生成用）
        self.agent = None          # Agentインスタンス（Layer1定期圧縮に使用）
        self.get_active_agent = None  # active_chat_agentを取得するコールバック（server.pyが設定）

        # Layer1定期圧縮の実行済みフラグ（当日は1回だけ実行する）
        self._last_layer1_compression_date: Optional[str] = None

        # schedule.json を読み込み（なければ空リストで初期化）
        self._load()

    def _load(self):
        """schedule.json からスケジュール定義を読み込む。ファイルがなければ空リストで初期化する。"""
        if self.schedule_file.exists():
            try:
                with open(self.schedule_file, "r", encoding="utf-8") as f:
                    self.schedules = json.load(f)
                print(f"スケジュール読み込み完了: {len(self.schedules)} 件")
            except Exception as e:
                print(f"警告: schedule.json の読み込みに失敗しました: {e}")
                self.schedules = []
        else:
            self.schedules = []
            self._save()

    def _save(self):
        """現在のスケジュール定義を schedule.json に書き出す。"""
        self.schedule_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.schedule_file, "w", encoding="utf-8") as f:
            json.dump(self.schedules, f, ensure_ascii=False, indent=2)

    def set_execute_callback(self, callback: Callable[[str, str, str], Awaitable[str]]):
        """
        タスク実行時のコールバックを設定する。server.pyの起動時に呼ばれる。

        Args:
            callback: async def callback(task_name: str, instruction: str, schedule_type: str) -> str
                      task_name: タスク名（ログ表示用）
                      instruction: MDファイルの内容（エージェントへの指示テキスト）
                      schedule_type: スケジュール種別（"daily" / "once" / "moonbeat"）
                      戻り値: エージェントの応答テキスト
        """
        self._execute_callback = callback

    def add_schedule(self, name: str, schedule_type: str, time_str: str,
                     task_file: str, interval_minutes: Optional[int] = None,
                     start_time: Optional[str] = None, end_time: Optional[str] = None) -> dict:
        """
        新しいスケジュールを追加してschedule.jsonに保存する。

        Args:
            name: タスク名（表示用）
            schedule_type: "daily"（毎日指定時刻）, "once"（1回限り）, または "interval"（一定間隔）
            time_str: "HH:MM"（daily）または "YYYY-MM-DD HH:MM"（once）。intervalではNone可
            task_file: タスクファイルのパス（workspace相対、例: "tasks/daily_report.md"）
            interval_minutes: 実行間隔（分）（intervalタイプ用）
            start_time: 実行開始時刻 "HH:MM"（intervalタイプ用）
            end_time: 実行終了時刻 "HH:MM"（intervalタイプ用）

        Returns:
            追加されたスケジュール辞書
        """
        schedule = {
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "schedule_type": schedule_type,
            "time": time_str if schedule_type == "daily" else None,
            "datetime": time_str if schedule_type == "once" else None,
            "interval_minutes": interval_minutes,
            "start_time": start_time,
            "end_time": end_time,
            "task_file": task_file,
            "enabled": True,
            "last_run": None,
        }
        self.schedules.append(schedule)
        self._save()
        print(f"スケジュール追加: {name} ({schedule_type} {time_str})")
        return schedule

    def remove_schedule(self, schedule_id: str) -> bool:
        """
        指定IDのスケジュールを削除する。

        Args:
            schedule_id: 削除対象のスケジュールID（8文字UUID）

        Returns:
            削除に成功したらTrue、該当IDが見つからなければFalse
        """
        before = len(self.schedules)
        self.schedules = [s for s in self.schedules if s["id"] != schedule_id]
        if len(self.schedules) < before:
            self._save()
            print(f"スケジュール削除: {schedule_id}")
            return True
        return False

    def list_schedules(self) -> list[dict]:
        """登録済みの全スケジュール定義を返す。"""
        return self.schedules

    def start(self):
        """バックグラウンド監視ループをasyncioタスクとして開始する。"""
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            print("スケジューラ開始")

    def stop(self):
        """バックグラウンド監視ループを停止する。"""
        if self._task:
            self._task.cancel()
            self._task = None
            print("スケジューラ停止")

    async def _loop(self):
        """
        毎分実行される監視ループ。

        登録済み全スケジュールの実行タイミングを判定し、
        該当するものがあれば_execute_task()を呼ぶ。
        スケジュールチェック後にMoonbeatのチェックも行う。
        分の境界に合わせて待機時間を調整する。
        """
        print("スケジューラ監視ループ開始")
        while True:
            try:
                now = datetime.now(JST)
                current_time = now.strftime("%H:%M")
                current_date = now.strftime("%Y-%m-%d")
                current_datetime = now.strftime("%Y-%m-%d %H:%M")

                for schedule in self.schedules:
                    if not schedule.get("enabled", True):
                        continue

                    should_run = False

                    if schedule["schedule_type"] == "daily":
                        # 毎日実行: 現在時刻が指定時刻と一致し、今日まだ実行していなければ
                        if schedule.get("time") == current_time:
                            last_run = schedule.get("last_run")
                            if not last_run or not last_run.startswith(current_date):
                                should_run = True

                    elif schedule["schedule_type"] == "once":
                        # 一回限り: 指定日時に一致し、まだ一度も実行していなければ
                        if schedule.get("datetime") == current_datetime:
                            if not schedule.get("last_run"):
                                should_run = True
                                
                    elif schedule["schedule_type"] == "interval":
                        # インターバル: 指定時間帯内であり、かつ前回実行から指定分数が経過していれば
                        interval = schedule.get("interval_minutes", 60)
                        start_t = schedule.get("start_time", "00:00")
                        end_t = schedule.get("end_time", "23:59")
                        
                        # 実行時間帯チェック
                        if start_t <= end_t:
                            is_in_time = start_t <= current_time <= end_t
                        else:
                            # 日またぎ（例：22:00〜06:00）
                            is_in_time = current_time >= start_t or current_time <= end_t
                            
                        if is_in_time:
                            last_run = schedule.get("last_run")
                            if not last_run:
                                # 一度も実行していなければ即実行
                                should_run = True
                            else:
                                # 前回実行時刻からinterval_minutes経過しているかチェック
                                try:
                                    last_run_dt = datetime.strptime(last_run, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
                                    if now >= last_run_dt + timedelta(minutes=interval):
                                        should_run = True
                                except Exception as e:
                                    print(f"時刻パースエラー ({last_run}): {e}")
                                    should_run = True

                    if should_run:
                        await self._execute_task(schedule)

                # Moonbeatの実行タイミングチェック
                await self._check_moonbeat(now)

                # Layer1定期圧縮のチェック（深夜3時・1日1回）
                await self._check_layer1_compression(now)
                # サリアの古い履歴をドロップ（2日分を残す）
                await self._check_salia_history_drop(now)
                await self._check_backup(now)
                await self._check_layer0_compression(now)

            except asyncio.CancelledError:
                print("スケジューラ監視ループを終了します")
                break
            except Exception as e:
                print(f"スケジューラエラー: {e}")

            # 次のチェックまで待機（分の境界に合わせて残り秒数を計算）
            now = datetime.now(JST)
            wait_seconds = 60 - now.second
            await asyncio.sleep(wait_seconds)

    async def _execute_task(self, schedule: dict):
        """
        スケジュールタスクを実行する。タスクファイルを読み込み、コールバック経由でエージェントに処理させる。

        Args:
            schedule: 実行対象のスケジュール辞書
        """
        task_file = schedule.get("task_file", "")
        task_name = schedule.get("name", "不明なタスク")

        print(f"\n{'='*50}")
        print(f"スケジュールタスク実行: {task_name}")
        print(f"タスクファイル: {task_file}")
        print(f"{'='*50}")

        # タスクファイル（MDファイル）をワークスペースから読み込む
        instruction = self.memory.read_file(task_file)
        if instruction is None:
            print(f"エラー: タスクファイルが見つかりません: {task_file}")
            return

        # last_run を現在時刻で更新（重複実行防止のため、実行開始時点で更新する）
        now = datetime.now(JST)
        schedule["last_run"] = now.strftime("%Y-%m-%d %H:%M")
        self._save()

        # server.pyのexecute_scheduled_taskコールバック経由でエージェントに実行させる
        if self._execute_callback:
            try:
                # タスク名・実行時刻のコンテキストを付与してエージェントに渡す
                instruction_with_context = (
                    f"【スケジュールタスク自動実行】\n"
                    f"タスク名: {task_name}\n"
                    f"実行時刻: {now.strftime('%Y-%m-%d %H:%M JST')}\n\n"
                    f"以下の指示書に従って行動してください:\n\n"
                    f"---\n{instruction}\n---"
                )
                response = await self._execute_callback(task_name, instruction_with_context, schedule["schedule_type"])
                print(f"タスク完了: {task_name}")
                print(f"応答: {response[:200]}...")
            except Exception as e:
                print(f"タスク実行エラー ({task_name}): {e}")
        else:
            print(f"警告: 実行コールバックが設定されていません")
            
        # 一回限り（once）のタスクは実行後にスケジュールから自動削除
        if schedule["schedule_type"] == "once":
            self.schedules.remove(schedule)
            self._save()
            tlog(f"[Schedule] 一回限りのタスク '{task_name}' を削除しました")

    async def _check_layer0_compression(self, now: datetime):
        """
        毎朝3時にLayer0圧縮を実行する。
        未圧縮ターン数がlayer0_scheduled_thresholdを超えていたら
        layer0_keep_turnsまで圧縮する。
        """
        if now.hour != 3 or now.minute != 0:
            return

        today = now.strftime("%Y-%m-%d")
        if getattr(self, '_last_layer0_compression_date', None) == today:
            return

        # アクティブエージェント（会話履歴を保持）を優先、なければglobal_agentで代替
        agent = (self.get_active_agent() if self.get_active_agent else None) or self.agent
        if agent is None:
            return

        import json as _json
        from pathlib import Path
        config_path = config_file("compression_config.json")
        config = _json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        threshold = config.get("layer0_scheduled_threshold", 80)
        keep_turns = config.get("layer0_keep_turns", 40)

        uncompressed = agent.context.count_uncompressed_turns()
        if uncompressed <= threshold:
            tlog(f"[Scheduler] Layer0圧縮不要 (未圧縮={uncompressed}件)")
            self._last_layer0_compression_date = today
            return

        tlog(f"[Scheduler] Layer0定期圧縮を開始します (未圧縮={uncompressed}件 → {keep_turns}件まで圧縮)")
        self._last_layer0_compression_date = today
        try:
            from pathlib import Path as _Path
            import json as _json2
            config2 = _json2.loads(config_file("compression_config.json").read_text(encoding="utf-8"))
            prompt_file = config2.get("layer0_prompt_file", "data/compression_prompt_layer0.txt")
            # data_root 基準で解決（"data/..." 相対値を data_root/data/... に。packaged で bundle 側を見ない）
            layer0_prompt = resolve_path(prompt_file).read_text(encoding="utf-8")
            # {{agent_name}} / {{user_honorific}} プレースホルダを実際の値に置換
            layer0_prompt = apply_prompt_placeholders(layer0_prompt, agent.agent_name, agent.honorific)

            target = uncompressed - keep_turns
            tlog(f"[Scheduler] Layer0定期圧縮: {target}件を圧縮します")
            for _ in range(target):
                turn = agent.context.extract_oldest_uncompressed_turn()
                if turn is None:
                    break
                start_idx, turn_msgs = turn
                # 単一ターン圧縮は agent._layer0_compress_turn に一元化（手動圧縮と共通）
                ok = await agent._layer0_compress_turn(start_idx, turn_msgs, layer0_prompt)
                if not ok:
                    break
            agent.context.save_state()

            # --- 記憶グラフ更新 ---
            from core.wyrd_network import load_graph, process_fact_buffer_async, save_graph, node_count
            graph = load_graph()

            async def llm_fn(prompt):
                response = await agent.llm.chat([{"role": "user", "content": prompt}], tools=None)
                return response.content or ""

            count = await process_fact_buffer_async(graph, embed_fn=agent._get_embedding, llm_fn=llm_fn, agent_name=agent.agent_name)
            if count > 0:
                tlog(f"[Scheduler] Wyrd Network: {count}件追加, {node_count(graph)}")
            tlog("[Scheduler] Layer0定期圧縮が完了しました")
          
        except Exception as e:
            tlog(f"[Scheduler] Layer0定期圧縮エラー: {e}")
          
    async def _check_backup(self, now: datetime):
        """
        毎朝3時に src フォルダを dst にバックアップする（作者母艦専用）。

        src/dst は環境変数で渡す（CG_DEV_BACKUP_SRC / CG_DEV_BACKUP_DST、任意で
        CG_GDRIVE_BACKUP_DST）。これは作者の母艦（柚月）専用のローカルバックアップで、
        robocopy /MIR（宛先ミラー）のため、他環境では「存在しない src でエラー連発」
        「宛先の別データを削除しに行く」といった害しか無い。

        判定に data_root != bundle_root（配布版判定）は使えない: OSS をソース配布した
        場合、ユーザーは作者と同じ dev 構成（venv + server.py、--data-root 無し）で動かす
        ため data_root == bundle_root となり、構造的に「作者母艦」と区別できない。
        よって明示フラグ CG_DEV_MACHINE=1（作者の .env のみに置く・非コミット非配布）が
        無い限り実行しない。これで packaged 配布版・OSS ソース実行のどちらでも自動 OFF。
        """
        import os
        if os.environ.get("CG_DEV_MACHINE") != "1":
            return

        if now.hour != 3 or now.minute != 0:
            return

        today = now.strftime("%Y-%m-%d")
        if getattr(self, '_last_backup_date', None) == today:
            return

        self._last_backup_date = today
        tlog("[Scheduler] バックアップを開始します")

        import subprocess

        def _robocopy(label: str, src: str, dst: str):
            """src を dst へ robocopy /MIR でミラーする。宛先ごとに独立して実行・例外処理。"""
            try:
                # 除外: venv（任意階層）／ビルド成果物 dist_build・liner\dist（再生成可能で巨大。
                # この2つだけで約10GB を占めるため、フルパス指定でミラー対象から外す）。
                excludes = ["venv", os.path.join(src, "dist_build"), os.path.join(src, "liner", "dist")]
                result = subprocess.run(
                    ["robocopy", src, dst, "/MIR", "/XD", *excludes, "/NFL", "/NDL", "/NJH", "/NJS"],
                    capture_output=True, text=True
                )
                if result.returncode <= 7:
                    tlog(f"[Scheduler] バックアップ完了({label})")
                else:
                    tlog(f"[Scheduler] バックアップエラー({label}): returncode={result.returncode}")
            except Exception as e:
                tlog(f"[Scheduler] バックアップエラー({label}): {e}")

        # src / 主宛先（D ドライブ等）は作者環境固有のため .env で渡す。
        # CG_DEV_MACHINE=1 だけ立てて SRC/DST が未設定だと過去はクラッシュしていたので、
        # 警告ログを出して skip する（他の正常な scheduler ループは止めない）。
        src = os.environ.get("CG_DEV_BACKUP_SRC")
        local_dst = os.environ.get("CG_DEV_BACKUP_DST")
        if not src or not local_dst:
            tlog("[Scheduler] バックアップ skip: CG_DEV_BACKUP_SRC / CG_DEV_BACKUP_DST が未設定")
            return

        # 1) ローカルドライブ（従来どおり / .env で宛先指定）
        _robocopy("ローカル", src, local_dst)

        # 2) Google Drive（任意）。Google Drive for Desktop を入れてサインインすると
        #    Drive がドライブ（例 G:\マイドライブ）としてマウントされる。ドライブレター
        #    もフォルダ名（"マイドライブ" 等）も環境・言語依存のため決め打ちせず、宛先は
        #    .env の CG_GDRIVE_BACKUP_DST で指定する（例: G:\マイドライブ\agent_backup\agent）。
        #    未設定なら Drive バックアップはスキップ。CG_DEV_MACHINE 同様 .env のみ・非配布。
        gdrive_dst = os.environ.get("CG_GDRIVE_BACKUP_DST")
        if gdrive_dst:
            _robocopy("GoogleDrive", src, gdrive_dst)
          
    async def _check_layer1_compression(self, now: datetime):
        """
        Layer1定期圧縮を深夜3時ちょうどに1日1回実行する。

        柚月を介さない純粋処理として agent.compress_layer1_scheduled() を直接呼び出す。
        - 3時ちょうど（hour==3, minute==0）かつ当日未実行の場合のみ発動
        - self.agent が None の場合は安全にスキップ
        - 実行済みフラグ（_last_layer1_compression_date）で当日の重複実行を防ぐ
        """
        # 3時ちょうどでなければスキップ
        if now.hour != 3 or now.minute != 0:
            return

        # 当日すでに実行済みならスキップ
        today = now.strftime("%Y-%m-%d")
        if self._last_layer1_compression_date == today:
            return

        # agentが未設定ならスキップ（startup_event完了前など）
        if self.agent is None:
            return

        self._last_layer1_compression_date = today
        tlog("[Scheduler] Layer1定期圧縮を開始します")
        try:
            await self.agent.compress_layer1_scheduled()
            tlog("[Scheduler] Layer1定期圧縮が完了しました")
        except Exception as e:
            tlog(f"[Scheduler] Layer1定期圧縮エラー: {e}")
    
    async def _check_salia_history_drop(self, now: datetime):
        """毎朝3時にサリアの古い会話履歴（2日より前）をドロップする。"""
        if now.hour != 3 or now.minute != 0:
            return
    
        today = now.strftime("%Y-%m-%d")
        if getattr(self, '_last_salia_drop_date', None) == today:
            return
    
        self._last_salia_drop_date = today
    
        agent = (self.get_active_agent() if self.get_active_agent else None) or self.agent
        if agent is None or not hasattr(agent, 'salia') or agent.salia is None:
            return
    
        try:
            agent.salia.drop_old_history()
            tlog("[Scheduler] サリアの古い履歴をドロップしました")
        except Exception as e:
            tlog(f"[Scheduler] サリア履歴ドロップエラー: {e}")
      
    async def _check_moonbeat(self, now: datetime):
        """
        Moonbeat（月動）の実行タイミングをチェックし、条件を満たしていればパルスを送信する。

        以下の条件を順にチェックする:
        1. moonbeat_config.jsonのenabled設定
        2. 現在時刻が設定された活動時間帯内か
        3. 前回パルスから設定間隔＋動的延長分が経過しているか
        4. コールバックが設定されているか
        5. スタミナ/エネルギーが最低値以上か
        全条件を満たした場合、時間帯・体力状態に応じたメッセージを選択し、
        フラッシュバック（過去記憶の断片）を付与してエージェントに送信する。
        """
        import random
        import os

        # --- 設定ファイル読み込み（data_root 基準。server.py と読み先を統一） ---
        from core.paths import data_file
        config_path = str(config_file("moonbeat_config.json"))
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        # OFF→ON に切り替わった瞬間にタイマーをリセットし、
        # 「ONにした時刻から interval 後」に最初の自動Moonbeatが発火するようにする。
        # 設定ページ・ダッシュボードのトグル・ファイル直接編集のいずれの経路でONにしても効く。
        enabled = config.get("enabled", False)
        if self._prev_enabled is False and enabled:
            self._last_moonbeat = now
            self._moonbeat_extension = 0
            tlog("[Moonbeat] 有効化を検知。タイマーをリセットしました（次回はinterval後）")
        self._prev_enabled = enabled

        if not enabled:
            return

        # --- 活動時間帯チェック（時間外ならスキップ） ---
        current_time = now.strftime("%H:%M")
        start_t = config.get("start_time", "07:00")
        end_t = config.get("end_time", "23:00")
        if start_t <= end_t:
            if not (start_t <= current_time <= end_t):
                return
        else:
            # 日またぎ（例：22:00〜06:00）
            if not (current_time >= start_t or current_time <= end_t):
                return

        # --- インターバルチェック（前回パルスから十分な時間が経過しているか） ---
        interval = config.get("interval_minutes", 30)
        if self._last_moonbeat:
            elapsed = (now - self._last_moonbeat).total_seconds() / 60.0
            # 基本間隔 + 動的延長分（トークン消費量に応じて前回設定された延長時間）
            if elapsed < interval + self._moonbeat_extension:
                return

        # コールバック未設定ならスキップ
        if not self._execute_callback:
            return

        # トークンカウントをリセット（次回の動的間隔計算用。スキップパスも含めリセットする）
        if self.vital_manager:
            self.vital_manager.data["_last_consumed_tokens"] = 0
            self.vital_manager._save()
      
        # --- スタミナ/エネルギーチェック（体力不足ならスキップ） ---
        if self.vital_manager:
            # 自然回復を適用してから判定
            self.vital_manager._apply_natural_recovery()
            self.vital_manager._save()
            stamina = self.vital_manager.data.get("stamina", 500)
            energy = self.vital_manager.data.get("energy", 50)
            min_stamina = config.get("min_stamina", 10)
            min_energy = config.get("min_energy", 5)
            if stamina < min_stamina or energy < min_energy:
                tlog(f"[Moonbeat] 体力不足のためスキップ (stamina={stamina}, energy={energy})")
                self._last_moonbeat = now
                return

        # --- パルスメッセージの選択（時間帯・体力に応じる）＋フラッシュバック付与 ---
        # 自動・手動の両方で共用するためメソッドに切り出している。
        pulse_message = await self._build_pulse_message(config, now)

        # パルス時刻を記録し、動的延長をリセット
        self._last_moonbeat = now
        self._moonbeat_extension = 0

        # 動的間隔計算用に現在のトークン消費量を記録
        total_tokens = self.vital_manager.data.get("_last_consumed_tokens", 0) if self.vital_manager else 0

        try:
            # server.pyのexecute_scheduled_taskコールバック経由でエージェントにパルスを送信
            callback_result = await self._execute_callback("Moonbeat", pulse_message, "moonbeat")

            # --- 動的間隔調整: トークン消費量に応じて次回Moonbeatまでの待機時間を延長 ---
            # 多くのトークンを消費したMoonbeat（長い思考やツール実行）の後は間隔を空ける
            dyn = config.get("dynamic_interval", {})
            if dyn.get("enabled", False) and self.vital_manager and callback_result != "SKIPPED":
                max_ext = dyn.get("max_extension_minutes", 60)
                token_per_min = dyn.get("token_per_minute", 1000)
                extension = min(total_tokens / token_per_min, max_ext)
                self._moonbeat_extension = extension
                if extension > 0:
                    interval = config.get("interval_minutes", 30)
                    tlog(f"[Moonbeat] 動的間隔: {total_tokens}トークン消費 → +{extension:.0f}分延長 (次回まで{interval + extension:.0f}分)")
        except Exception as e:
            tlog(f"[Moonbeat] エラー: {e}")


    async def _build_pulse_message(self, config: dict, now: Optional[datetime] = None) -> str:
        """時間帯・体力状態に応じたパルスメッセージを選び、確率でフラッシュバックを付与して返す。

        自動Moonbeat（_check_moonbeat）と手動発火（trigger_manual_moonbeat）で共用する。

        Args:
            config: moonbeat_config.json の内容
            now: 基準時刻。省略時は現在時刻（手動発火用）
        Returns:
            パルスメッセージ文字列（必要に応じてフラッシュバックを末尾に連結済み）
        """
        import random
        from core.paths import data_file

        if now is None:
            now = datetime.now(JST)

        # data/moonbeat_messages.json から時間帯・体力状態に応じたメッセージプールを選択する（data_root 基準）
        messages_path = str(data_file("moonbeat_messages.json"))
        try:
            with open(messages_path, 'r', encoding='utf-8') as f:
                messages = json.load(f)
        except FileNotFoundError:
            tlog(f"[Moonbeat] 警告: moonbeat_messages.json が見つかりません ({messages_path})。フォールバックメッセージを使用します。")
            messages = {}
        except json.JSONDecodeError as e:
            tlog(f"[Moonbeat] 警告: moonbeat_messages.json のJSON解析に失敗 ({e})。フォールバックメッセージを使用します。")
            messages = {}

        if self.vital_manager:
            energy = self.vital_manager.data.get("energy", 50)
            energy_max = self.vital_manager.data.get("config", {}).get("energy_max", 50)
            stamina = self.vital_manager.data.get("stamina", 500)
            stamina_max = self.vital_manager.data.get("config", {}).get("stamina_max", 500)

            # 体力が低い場合は専用のメッセージプールを使用
            if energy < energy_max * 0.3:
                pool = messages.get("low_energy", ["少し疲れた…でも何かできることはあるかな。"])
            elif stamina < stamina_max * 0.2:
                pool = messages.get("low_stamina", ["今日はたくさん動いた。そろそろ休もうかな。"])
            else:
                # 通常時: 現在の時間帯に対応するメッセージプールを選択
                hour = now.hour
                time_periods = config.get("time_periods", [])
                period = "night"  # デフォルト（どの時間帯にも該当しない場合）
                for tp in time_periods:
                    if hour < tp["until"]:
                        period = tp["period"]
                        break
                pool = messages.get(period, ["自由時間です。"])
        else:
            # VitalManager未設定時: 時間帯のみでメッセージを選択
            hour = now.hour
            time_periods = config.get("time_periods", [])
            period = "night"
            for tp in time_periods:
                if hour < tp["until"]:
                    period = tp["period"]
                    break
            pool = messages.get(period, ["自由時間です。"])

        pulse_message = random.choice(pool)
        tlog(f"[Moonbeat] パルス送信: {pulse_message}")

        # --- フラッシュバック: 一定確率で過去の記憶の断片を生成してメッセージに付与 ---
        flashback = await self._generate_flashback(config, self.rag_db)
        if flashback:
            pulse_message += flashback

        return pulse_message


    async def trigger_manual_moonbeat(self) -> str:
        """手動Moonbeat発火（ダッシュボードのボタン等から呼ぶ）。

        自動Moonbeatのゲート（enabled / 活動時間帯 / interval / 体力）をすべて無視して
        即座に発火する。ユーザーの明示操作のため「直近5分の会話スキップ」も無視する
        （execute_scheduled_task に manual=True を渡すことで実現）。
        ただし睡眠中（sleep/nap）は起こさないためスキップし、他処理の実行中も多重実行を避ける。

        実際に発火できた場合のみ _last_moonbeat をリセットし、次回の自動Moonbeatを
        「発火時刻から interval 後」に揃える。

        Returns:
            "fired"        … 発火した（タイマーをリセット済み）
            "skipped"      … 睡眠中のためスキップ
            "busy"         … 他処理の実行中のためスキップ
            "no_callback"  … コールバック未設定（通常は起こらない）
        """
        if not self._execute_callback:
            return "no_callback"

        from core.paths import config_file

        # 設定ファイルを読み込む（メッセージ選択・フラッシュバックの確率に使用）
        try:
            with open(str(config_file("moonbeat_config.json")), 'r', encoding='utf-8') as f:
                config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            config = {}

        pulse_message = await self._build_pulse_message(config)

        # manual=True で発火本体を呼ぶ（5分会話チェックはバイパス、睡眠/多重実行チェックは維持）
        result = await self._execute_callback("Moonbeat", pulse_message, "moonbeat", manual=True)

        if result == "SKIPPED":
            tlog("[Moonbeat] 手動発火: 睡眠中のためスキップしました")
            return "skipped"
        if result == "":
            tlog("[Moonbeat] 手動発火: 処理中のためスキップしました")
            return "busy"

        # 実際に発火した → タイマーをリセットし、次回自動Moonbeatをinterval後に揃える
        self._last_moonbeat = datetime.now(JST)
        self._moonbeat_extension = 0
        tlog("[Moonbeat] 手動発火しました。タイマーをリセットしました（次回はinterval後）")
        return "fired"


    async def _generate_flashback(self, config: dict, rag_db) -> str:
        """
        フラッシュバック（過去記憶の断片）テキストを生成する。

        排他1枠で3種類のうち1つを選ぶ:
        - event_db フラッシュバック（既存の動作）
        - note_fragment フラッシュバック（新規・雑記帳由来）
        - 何もなし

        確率は config.flashback.event_db_probability と note_fragment_probability で制御される。

        Args:
            config: moonbeat_config.jsonの内容
            rag_db: RAGデータベースインスタンス

        Returns:
            "<flashback>...</flashback>" または "<note_fragment>...</note_fragment>" 形式の文字列、または空文字
        """
        import random

        fb_config = config.get("flashback", {})
        tlog(f"[Flashback DEBUG] fb_config={fb_config}")
        if not fb_config.get("enabled", False):
            return ""

        # 排他1枠の確率判定
        event_db_prob = fb_config.get("event_db_probability", 0.20)
        note_prob = fb_config.get("note_fragment_probability", 0.15)
        roll = random.random()
        tlog(f"[Flashback DEBUG] roll={roll:.3f}, event_db_prob={event_db_prob}, note_prob={note_prob}")

        if roll < event_db_prob:
            return await self._generate_event_db_flashback(config, rag_db)
        elif roll < event_db_prob + note_prob:
            return await self._generate_note_fragment(config)
        else:
            return self._generate_tips(config)

    def _generate_tips(self, config: dict) -> str:
        """
        flashbackもnote_fragmentも発火しなかった残り枠で、
        data/tips.txtからランダムに1つ選んで<tips>タグで返す。

        tips.txtは「---」区切りで複数のtipsを記述する。各ブロック内は改行自由。
        """
        import random
        import json as _json
        from pathlib import Path

        try:
            # tips のオンオフ（config/tips_config.json の "enabled"）。平文 tips.txt には
            # フラグを入れられないため、有効/無効は別ファイルで管理する。毎発火時に読むため即時反映。
            # ファイルが無い/壊れている場合は従来通り有効扱い（デフォルト true）。
            try:
                tips_cfg_path = config_file("tips_config.json")
                if tips_cfg_path.exists():
                    if not _json.loads(tips_cfg_path.read_text(encoding="utf-8")).get("enabled", True):
                        return ""
            except (OSError, ValueError):
                pass

            tips_path = config_file("tips.txt")
            if not tips_path.exists():
                tlog("[Flashback DEBUG] tips: data/tips.txt が存在しない")
                return ""

            content = tips_path.read_text(encoding="utf-8")
            # 「---」で分割、各ブロックをstrip、空ブロックは除外
            blocks = [b.strip() for b in content.split("---")]
            blocks = [b for b in blocks if b]

            if not blocks:
                tlog("[Flashback DEBUG] tips: 有効なtipsが0件")
                return ""

            selected = random.choice(blocks)
            tlog(f"[Flashback] tips: ヒントを注入")
            return f"\n\n<tips>\n{selected}\n</tips>"

        except Exception as e:
            tlog(f"[Flashback] tips エラー: {e}")
            return ""
  
    async def _generate_event_db_flashback(self, config: dict, rag_db) -> str:
        """
        event_db.jsonから高スコアイベントを引いてフラッシュバックを生成する。
        旧 _generate_flashback の中身を分離したもの。
        """
        import random
        import json

        fb_config = config.get("flashback", {})
        if rag_db is None:
            tlog("[Flashback DEBUG] event_db: rag_dbが利用不可")
            return ""

        # active_agent優先で取得
        agent = (self.get_active_agent() if self.get_active_agent else None) or self.agent
        if agent is None:
            tlog("[Flashback DEBUG] event_db: agentが利用不可")
            return ""

        try:
            workspace = str(self.memory.workspace)
            event_db_path = f"{workspace}/memory/event_db.json"
            with open(event_db_path, "r", encoding="utf-8") as f:
                events = json.load(f)

            min_score = fb_config.get("min_score", 50)
            candidates = [e for e in events if e.get("score", 0) >= min_score]
            if not candidates:
                return ""
            event = random.choice(candidates)
            date = event.get("date", "")

            results = rag_db.search("daily_memories", event.get("text", ""), n_results=3)
            matched = [r for r in results if r.get("metadata", {}).get("date", "") == date]
            if not matched:
                matched = results
            if not matched:
                tlog(f"[Flashback DEBUG] event_db: RAG検索結果なし（date={date}）")
                return ""
            memory_text = matched[0].get("document", "")[:500]

            prompt = fb_config.get("prompt", "以下の記憶から、100文字以内の印象的な一文を生成してください。")
            summary_messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": memory_text},
            ]
            try:
                direct_response = await agent.llm.client.chat.completions.create(
                    model=agent.llm.model,
                    messages=summary_messages,
                    max_tokens=200,
                    temperature=0.7,
                )
                flashback_text = (direct_response.choices[0].message.content or "").strip()

                if not flashback_text:
                    tlog(f"[Flashback DEBUG] event_db: LLMが空文字を返した")
                if flashback_text:
                    tlog(f"[Flashback] event_db: {date}の記憶を注入")
                    return f"\n\n<flashback>\n{flashback_text}\n</flashback>"
            except Exception as e:
                tlog(f"[Flashback] event_db エラー: {e}")
                return ""
        except Exception as e:
            tlog(f"[Flashback] event_db 外部エラー: {e}")
            return ""

    async def _generate_note_fragment(self, config: dict) -> str:
        """
        雑記帳からのノートフラグメントを生成する。

        1. workspace/notes/ から当日除外でファイル一覧を取得
        2. 重み付き（age_days^0.7 * file_size）で3ファイル選ぶ
        3. 各ファイルからチャンクを1つランダム抽出
        4. サリアに渡して1つ選ばせる
        5. 選ばれた断片を <note_fragment> タグで返す
        """
        import random
        import re as _re
        from datetime import date as _date
        from pathlib import Path

        fb_config = config.get("flashback", {})

        # サリアが必要（active_agent優先）
        agent = (self.get_active_agent() if self.get_active_agent else None) or self.agent
        if agent is None or not hasattr(agent, 'salia') or agent.salia is None:
            tlog("[Flashback DEBUG] note_fragment: サリアが利用不可")
            return ""

        # 雑記帳ディレクトリ
        notes_dir = Path(self.memory.workspace) / "notes"
        if not notes_dir.exists():
            tlog(f"[Flashback DEBUG] note_fragment: notesディレクトリ無し: {notes_dir}")
            return ""

        # 当日除外でファイル一覧
        today_str = _date.today().strftime("%Y-%m-%d")
        today_filename = f"note_{today_str}.md"
        note_files = [
            f for f in notes_dir.glob("note_*.md")
            if f.name != today_filename
        ]
        if not note_files:
            tlog(f"[Flashback DEBUG] note_fragment: ファイルなし（今日除外後）")
            return ""
        tlog(f"[Flashback DEBUG] note_fragment: {len(note_files)}ファイル候補")

        # 重み計算: (age_days ** age_bias) * file_size
        age_bias = fb_config.get("note_fragment_age_bias", 0.7)
        today = _date.today()
        weights = []
        for f in note_files:
            try:
                file_date = _date.fromisoformat(f.stem[5:15])  # "note_YYYY-MM-DD"
                age_days = max(1, (today - file_date).days)
                file_size = max(1, f.stat().st_size)
                weight = (age_days ** age_bias) * file_size
                weights.append(weight)
            except (ValueError, IndexError, OSError):
                weights.append(1.0)

        # 重み付きランダムで最大N個選ぶ（重複なし）
        n_candidates = fb_config.get("note_fragment_candidate_files", 3)
        n_candidates = min(n_candidates, len(note_files))

        selected_files = []
        remaining_files = list(note_files)
        remaining_weights = list(weights)
        for _ in range(n_candidates):
            if not remaining_files:
                break
            idx = random.choices(range(len(remaining_files)), weights=remaining_weights, k=1)[0]
            selected_files.append(remaining_files[idx])
            remaining_files.pop(idx)
            remaining_weights.pop(idx)

        # 各ファイルからチャンクを1つランダム抽出
        candidates = []
        for f in selected_files:
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue

            chunks = self._split_note_into_chunks(content)
            if not chunks:
                continue

            chunk = random.choice(chunks)
            candidates.append({
                "source": f.name,
                "content": chunk,
            })

        if not candidates:
            tlog(f"[Flashback DEBUG] note_fragment: 候補チャンクが0件")
            return ""
        tlog(f"[Flashback DEBUG] note_fragment: {len(candidates)}件の候補チャンクをサリアへ")

        # サリアに選ばせる
        try:
            selected_idx = await agent.salia.select_note_fragment(candidates)
        except Exception as e:
            tlog(f"[Flashback] note_fragment サリア選択エラー: {e}")
            return ""

        if selected_idx is None:
            tlog(f"[Flashback] note_fragment: サリアが「ふさわしい候補なし」と判定")
            return ""

        chunk = candidates[selected_idx]
        source = chunk["source"]
        content = chunk["content"]

        # 長すぎる場合は切る
        max_chars = fb_config.get("note_fragment_max_chars", 300)
        if len(content) > max_chars:
            content = content[:max_chars].rstrip() + "…"

        tlog(f"[Flashback] note_fragment: {source} の断片を注入")
        return f"\n\n<note_fragment>\n[{source}]\n{content}\n</note_fragment>"

    def _split_note_into_chunks(self, content: str) -> list[str]:
        """
        雑記帳の内容を粗くチャンク分割する。

        - まず ## 見出しで分割（改行なしで ## が現れるケースも検出）
        - 長すぎるチャンクは空行2連続で再分割
        - 空白だけ・極端に短い断片は除外
        """
        import re as _re

        if not content or not content.strip():
            return []

        # ## の前に改行を強制的に入れて分割を確実にする（行頭でない ## も拾うため）
        normalized = _re.sub(r'(?<!\n)##', r'\n##', content)

        # ## で分割
        parts = _re.split(r'\n##\s*', normalized)

        chunks = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # 長すぎるチャンクは空行2連続で更に分割
            if len(part) > 1000:
                sub_parts = _re.split(r'\n\s*\n\s*\n+', part)
                for sub in sub_parts:
                    sub = sub.strip()
                    if len(sub) >= 30:  # 短すぎるものは除外
                        chunks.append(sub)
            elif len(part) >= 30:
                chunks.append(part)

        return chunks
