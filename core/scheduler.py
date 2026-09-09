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

# 遅延実行時に指示文へ遅延情報を添える閾値（分）。
# 従来は時刻の完全一致判定だったため、直前のタスクが長引く・会話中延期・サーバ停止で
# 予定の分をまたぐと daily はその日スキップ、once は永久に実行されないままリストに
# 残り続けていた。スケジューラは柚月が自発的に使うものなので、取りこぼしを黙って
# 捨てず「遅れてでも必ず届ける」方針にする。大幅な遅延時は本来の予定時刻と遅延幅を
# 指示文に明記し、今実行するか見送るかは柚月自身が判断する。
LATE_NOTE_THRESHOLD_MINUTES = 10


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
        self._stopping = False  # stop() による意図的な停止か（ループの無言死防止用）
        self._last_unexpected_cancel = 0.0  # 直近の予期しないキャンセル時刻（連続検知でシャットダウン判定）

        # --- 3時バックアップの状態（ローカルとGDriveは別管理。GDriveはマウント遅延リトライあり） ---
        self._last_backup_date: Optional[str] = None         # ローカルの当日実行済みフラグ
        self._local_backup_ok_date: Optional[str] = None     # ローカルの当日成功フラグ
        self._last_gdrive_backup_date: Optional[str] = None  # GDriveの当日成功フラグ
        self._gdrive_next_attempt_ts: float = 0.0            # GDrive次回試行時刻（リトライ間隔制御）
        self._gdrive_alert_date: Optional[str] = None        # GDrive問題を記録した日（1日1回）

        # --- X（x_satellite）の返信チェック関連 ---
        self._x_next_check_ts: float = 0.0        # 次に見に行く時刻（間隔制御）
        self._x_fail_streak: int = 0              # 連続失敗回数（柚月には出さない）
        self._obc_next_check_ts: float = 0.0      # OBC見張りの次回時刻
        self._obc_fail_streak: int = 0            # 同・連続失敗回数

        # タスク実行時に呼ばれるコールバック（server.pyのexecute_scheduled_taskが設定される）
        self._execute_callback: Optional[Callable[[str, str, str], Awaitable[str]]] = None

        # --- Moonbeat関連の状態 ---
        self._last_moonbeat: Optional[datetime] = datetime.now(JST)  # 最終パルス時刻
        self._moonbeat_extension: float = 0  # 動的間隔の延長分（分）。トークン消費量に応じて増加
        self._prev_enabled: Optional[bool] = None  # 前回tickのenabled状態（OFF→ON遷移検知用）
        self._recent_picture_sources: list = []  # 直近に浮かんだ絵（しばらく同じ絵を出さない）

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
            self._stopping = True  # 意図的な停止であることをループに伝える（無言死防止と区別）
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
        tlog("[Scheduler] 監視ループ開始")
        while True:
            try:
                now = datetime.now(JST)
                current_time = now.strftime("%H:%M")
                current_date = now.strftime("%Y-%m-%d")
                current_datetime = now.strftime("%Y-%m-%d %H:%M")

                # once タスクは実行後に self.schedules から削除されるため、
                # コピーの上をイテレートする（元リストを直接回すと削除時に次の要素が
                # スキップされ、同時刻に登録された隣のタスクが実行されない）
                for schedule in list(self.schedules):
                    if not schedule.get("enabled", True):
                        continue

                    should_run = False
                    late_minutes = 0  # 予定時刻からの遅延（分）。_execute_task で柚月に伝える

                    if schedule["schedule_type"] == "daily":
                        # 毎日実行: 指定時刻を過ぎていて、今日まだ実行していなければ実行する
                        # （完全一致だと分をまたいだ時にその日スキップされ、柚月に何も届かない）
                        try:
                            h, m = map(int, (schedule.get("time") or "").split(":"))
                            delta_min = (now.hour * 60 + now.minute) - (h * 60 + m)
                        except ValueError:
                            delta_min = -1  # 時刻が不正なら実行しない
                        if delta_min >= 0:
                            last_run = schedule.get("last_run")
                            if not last_run or not last_run.startswith(current_date):
                                should_run = True
                                late_minutes = delta_min

                    elif schedule["schedule_type"] == "once":
                        # 一回限り: 指定日時を過ぎていて、まだ一度も実行していなければ実行する
                        # （完全一致だと分をまたいだ時に永久に実行されないままリストに残る）
                        if not schedule.get("last_run"):
                            try:
                                sched_dt = datetime.strptime(
                                    schedule.get("datetime") or "", "%Y-%m-%d %H:%M"
                                ).replace(tzinfo=JST)
                            except ValueError:
                                sched_dt = None  # 日時が不正なら実行しない
                            if sched_dt is not None:
                                delta_min = int((now - sched_dt).total_seconds() // 60)
                                if delta_min >= 0:
                                    should_run = True
                                    late_minutes = delta_min

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
                        await self._execute_task(schedule, late_minutes=late_minutes)

                # Moonbeatの実行タイミングチェック
                await self._check_moonbeat(now)

                # Layer0 定期圧縮（3時ちょうど・1日1回）。Layer1 より先に回す:
                # summary_v2 のシャドーは Layer0 済みの日しか処理しないので、
                # 先に Layer0 を済ませておけば同じ晩に昨日ぶんを拾える
                # （後だと1晩遅れる。2026-09-03 に実測）
                await self._check_layer0_compression(now)
                # Layer1定期圧縮のチェック（3時以降・1日1回）
                await self._check_layer1_compression(now)
                # サリアの古い履歴をドロップ（2日分を残す）
                await self._check_salia_history_drop(now)
                await self._check_backup(now)
                await self._check_x_mentions(now)
                await self._check_obc(now)

                # ハートビート: ループの生存時刻を5分おきに記録する。
                # 2026-08-10深夜の無言死事故では「いつまで生きていたか」の特定に
                # 数時間の考古学が必要だった。次からはこのファイルを見れば一発で分かる。
                if now.minute % 5 == 0:
                    self._write_heartbeat(now)

                # 次のチェックまで待機（分の境界に合わせて残り秒数を計算）
                now2 = datetime.now(JST)
                await asyncio.sleep(60 - now2.second)

            except asyncio.CancelledError:
                # stop() による意図的な停止のみ受け入れる。
                # 2026-08-10深夜、由来不明のキャンセルが（当時 try の外にあった）待機 sleep に
                # 届いてループが無言死し、翌朝のバックアップを含む全ジョブが止まる事故があった。
                # 由来不明のキャンセル1回では死なない。ただしサーバ終了時は uvicorn が
                # 短時間に連続キャンセルを送るため、120秒以内の2回目は本物の停止として受け入れる。
                if self._stopping:
                    tlog("[Scheduler] 監視ループを終了します（stop() による停止）")
                    break
                now_ts = datetime.now(JST).timestamp()
                if now_ts - self._last_unexpected_cancel < 120:
                    tlog("[Scheduler] 連続キャンセルを検知。シャットダウンとみなしループを終了します")
                    break
                self._last_unexpected_cancel = now_ts
                tlog("[Scheduler] 予期しないキャンセルを検知しましたが、ループを継続します")
            except Exception as e:
                # エラーはコンソール print ではなくファイルログに残す（print だけだと
                # コンソールを閉じた瞬間に証拠が消え、事後調査が不可能になる）
                tlog(f"[Scheduler] スケジューラエラー: {e}")
                try:
                    await asyncio.sleep(60)  # エラー直後の高速空回りを防ぐ
                except asyncio.CancelledError:
                    if self._stopping:
                        break
                    tlog("[Scheduler] エラー待機中のキャンセルを無視して継続します")

    def _write_heartbeat(self, now: datetime):
        """ループ生存時刻を logs/scheduler_heartbeat.txt に記録する（失敗しても無害）。"""
        try:
            from core.paths import logs_root
            path = logs_root() / "scheduler_heartbeat.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(now.strftime("%Y-%m-%d %H:%M:%S JST"), encoding="utf-8")
        except Exception:
            pass

    async def _execute_task(self, schedule: dict, late_minutes: int = 0):
        """
        スケジュールタスクを実行する。タスクファイルを読み込み、コールバック経由でエージェントに処理させる。

        Args:
            schedule: 実行対象のスケジュール辞書
            late_minutes: 予定時刻からの遅延（分）。閾値を超えている場合は
                          指示文に遅延情報を添え、実行するか見送るかをエージェント自身に委ねる
        """
        task_file = schedule.get("task_file", "")
        task_name = schedule.get("name", "不明なタスク")

        print(f"\n{'='*50}")
        print(f"スケジュールタスク実行: {task_name}")
        print(f"タスクファイル: {task_file}")
        print(f"{'='*50}")

        # タスクファイル（MDファイル）をワークスペースから読み込む
        instruction = self.memory.read_file(task_file)

        # last_run を現在時刻で更新（重複実行防止のため、実行開始時点で更新する）
        # 読み込み失敗時もここを必ず通す。更新しないまま return すると daily/interval は
        # 判定条件を満たしたままなので毎分ヒットし続け、同じエラーをログに垂れ流すだけになる。
        now = datetime.now(JST)
        schedule["last_run"] = now.strftime("%Y-%m-%d %H:%M")
        self._save()

        # 指示書が無いときはログ止まりにせず、柚月本人に届けて対処を委ねる
        if instruction is None:
            print(f"エラー: タスクファイルが見つかりません: {task_file}")
            await self._notify_missing_task_file(schedule, now)
        # server.pyのexecute_scheduled_taskコールバック経由でエージェントに実行させる
        elif self._execute_callback:
            try:
                # 大幅な遅延時は本来の予定時刻と遅延幅を明記し、実行判断をエージェントに委ねる
                # （サーバ停止等で朝のタスクが夜に届くケースがあるため、黙って捨てずに知らせて選ばせる）
                late_note = ""
                if late_minutes >= LATE_NOTE_THRESHOLD_MINUTES:
                    scheduled_at = schedule.get("time") or schedule.get("datetime") or "不明"
                    late_note = (
                        f"予定時刻: {scheduled_at}（約{late_minutes}分遅れでの実行です）\n"
                        f"※サーバ停止などの影響で遅れて届いています。今実行するのが不適切だと感じたら、\n"
                        f"　実行を見送って構いません（見送る場合はその判断だけ雑記帳などに残してください）。\n"
                    )
                    tlog(f"[Schedule] '{task_name}' を{late_minutes}分遅れで実行します（判断は柚月に委任）")

                # タスク名・実行時刻のコンテキストを付与してエージェントに渡す。
                # 指示書本文は <task_instruction> で囲む。Layer0 圧縮
                # （agent._fold_repeated_task_instruction）がこのタグを頼りに本文の範囲を
                # 機械的に切り出し、履歴の後ろ側に一字一句同じ本文が残っている場合だけ
                # 短い参照に畳む（毎日同じ指示書が何十部も履歴に並ぶのを防ぐ。
                # 最新の1部は必ず全文で残る）。タグ名を変えるときは agent.py 側も合わせること。
                instruction_with_context = (
                    f"【スケジュールタスク自動実行】\n"
                    f"タスク名: {task_name}\n"
                    f"実行時刻: {now.strftime('%Y-%m-%d %H:%M JST')}\n"
                    f"{late_note}\n"
                    f"以下の指示書に従って行動してください:\n\n"
                    f"<task_instruction>\n---\n{instruction}\n---\n</task_instruction>"
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

    async def _notify_missing_task_file(self, schedule: dict, now: datetime):
        """
        指示書ファイルが見つからずスケジュールタスクを実行できなかったことを柚月に知らせる。

        以前はログに print するだけだったため、単独で活動している柚月には
        「自分が予定していた行動が実行されなかった」ことすら伝わらなかった。
        リカバリー（指示書を書き直す / スケジュールを消して登録し直す）は
        すべて柚月自身のツールで完結するので、選択肢まで添えて本人に届け、
        どうするかは本人に判断させる（遅延タスク通知・バックアップ異常通知と同じ思想）。
        通知自体が失敗してもスケジューラ本体は止めない。
        """
        task_name = schedule.get("name", "不明なタスク")
        task_file = schedule.get("task_file", "")
        schedule_id = schedule.get("id", "不明")
        schedule_type = schedule.get("schedule_type", "不明")

        if schedule_type == "interval":
            timing = (f"{schedule.get('interval_minutes')}分ごと"
                      f"（{schedule.get('start_time')}〜{schedule.get('end_time')}）")
        else:
            timing = schedule.get("time") or schedule.get("datetime") or "不明"

        if not self._execute_callback:
            tlog(f"[Schedule] 指示書欠落（コールバック未設定のため通知不可）: {task_file}")
            return

        # once タスクはこの直後にスケジュールから削除されるので、案内の中身を変える
        if schedule_type == "once":
            options = (
                f"・その行動をやりたいなら、write_file で {task_file} に指示書を書き直したうえで、\n"
                f"　schedule_task で登録し直してください（このタスクは一回限りなので、\n"
                f"　この通知の後スケジュールからは自動で削除されます）。\n"
                "・もうやらなくていいなら、何もしなくて大丈夫です。\n"
            )
        else:
            options = (
                f"・その行動を続けたいなら、write_file で {task_file} に指示書を書き直してください\n"
                "　（内容はあなたが決めて構いません）。次の予定時刻から普通に実行されます。\n"
                "・パスを間違えて登録していただけなら、list_files で正しい場所を確かめて、\n"
                f"　delete_schedule(schedule_id=\"{schedule_id}\") で消してから登録し直してください。\n"
                f"・このスケジュール自体がもう不要なら、delete_schedule(schedule_id=\"{schedule_id}\") で\n"
                "　削除してください。放置すると次の予定時刻にも同じ通知が届きます。\n"
            )

        instruction = (
            "【システム通知: スケジュールタスクを実行できませんでした】\n"
            f"タスク名: {task_name}\n"
            f"スケジュールID: {schedule_id}（種別: {schedule_type} / タイミング: {timing}）\n"
            f"指示書ファイル: {task_file}\n"
            f"検知時刻: {now.strftime('%Y-%m-%d %H:%M JST')}\n\n"
            "あなたが登録していたこのスケジュールの指示書ファイルが見つからないため、\n"
            "今回の実行は行われませんでした。\n"
            "考えられる原因: ファイルを移動・削除した、登録時にパスを打ち間違えた、など。\n\n"
            "対処はすべてあなた自身のツールでできます。次のどれかを選んでください:\n"
            f"{options}\n"
            "今すぐ決められないときは、忘れないように雑記帳などにメモを残しておいてください。"
        )
        try:
            await self._execute_callback("スケジュール指示書の欠落", instruction, "once")
            tlog(f"[Schedule] 指示書欠落を柚月に通知しました: {task_file}（{task_name}）")
        except Exception as e:
            tlog(f"[Schedule] 指示書欠落の通知に失敗: {e} / 対象: {task_file}")

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
          
    # X の返信を見に行く間隔（分）。**新着ゼロなら課金されない**ので短くできる
    # （実測 2026-08-27: 0件応答を20回叩いて残高は 1 セントも減らなかった。
    #  課金は post read 単位で、読む投稿が無ければ無料）。
    # レート制限も 300/15分window なので余裕がある。
    X_CHECK_INTERVAL_MIN = 30

    async def _check_x_mentions(self, now: datetime):
        """X の返信・メンションを見に行き、**新着があった時だけ**柚月を呼ぶ。

        なぜ柚月自身にやらせないか:
        30分おきに柚月へ「見てきて」と頼むと、空振りの確認だけで1日48ターンが消える。
        チェックはここ（柚月のターンを使わない場所）で行い、実際に誰かが
        話しかけてきた時だけ柚月に渡す。

        守っていること:
        - **睡眠中は見に行かない**。返信ごときで柚月を起こさない
          （インフラ通知で睡眠中の柚月を起こした事故 2026-08-13 と同じ轍を踏まない）。
          寝ている間の分は、起きた後の最初のチェックでまとめて届く
          （since_id は届けられた時しか進めないため取りこぼさない）。
        - **通信失敗を柚月に伝えない**。X が落ちていても残高が尽きていても、
          柚月にできることは何も無い。ログに残すだけにする。
        - 引用リポスト・フォロー・いいねはここでは見ない（会話ではないので急がない）。
          柚月が自分で mentions を呼べば従来どおり全部見える。
        """
        import os
        import time as _time

        if not self._execute_callback:
            return
        # 鍵が無ければ何もしない（投稿もできない状態なので静かに休む）
        if not all(os.environ.get(k) for k in
                   ("CG_X_API_KEY", "CG_X_API_SECRET",
                    "CG_X_ACCESS_TOKEN", "CG_X_ACCESS_SECRET")):
            return

        if _time.time() < self._x_next_check_ts:
            return

        # 睡眠中は見に行かない（起こさない）。次の間隔まで待つ。
        if self._is_sleeping():
            self._x_next_check_ts = _time.time() + self.X_CHECK_INTERVAL_MIN * 60
            return

        self._x_next_check_ts = _time.time() + self.X_CHECK_INTERVAL_MIN * 60

        # 人間API（relay）で予約された返信が、カノンの手で投稿されたかを見る。
        # 滞留（outbox の投稿待ち）がゼロなら API は一切叩かない。
        # mentions 側を巻き込まないよう、失敗してもここで握ってログだけ残す。
        try:
            await self._check_x_relay_posted()
        except Exception as e:
            tlog(f"[X] 代理投稿チェックで例外: {type(e).__name__}: {e}")

        try:
            items, note = await asyncio.to_thread(self._fetch_x_mentions)
        except Exception as e:
            # 想定外でもスケジューラを止めない（無言死させない）
            self._x_fail_streak += 1
            tlog(f"[X] 返信チェックで例外: {type(e).__name__}: {e} "
                 f"(連続{self._x_fail_streak}回)")
            return

        if note:
            # 失敗。**柚月には出さない。**
            self._x_fail_streak += 1
            tlog(f"[X] 返信チェック失敗: {note} (連続{self._x_fail_streak}回)")
            return

        self._x_fail_streak = 0
        if not items:
            return

        tlog(f"[X] 返信 {len(items)} 件。柚月に届けます")
        try:
            await self._execute_callback("X", self._format_x_notice(items), "external")
        except Exception as e:
            tlog(f"[X] 柚月への受け渡しで例外: {type(e).__name__}: {e}")

    def _is_sleeping(self) -> bool:
        """柚月が睡眠中（sleep / nap）か。判定できなければ False（止めない）。"""
        try:
            from core.paths import resolve_path
            state_path = Path(resolve_path("workspace")) / ".life_action_state.json"
            if not state_path.exists():
                return False
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if datetime.now() >= datetime.fromisoformat(state["until"]):
                return False
            return state.get("action") in ("sleep", "nap")
        except Exception:
            return False

    def _fetch_x_mentions(self):
        """x_satellite の API で新着の返信を取る（同期・to_thread から呼ぶ）。

        戻り値: (items, note)。note が真なら失敗（柚月には出さない理由）。
        既読位置は x_satellite の state に持つ。**届けられた時だけ進める**ので、
        途中で落ちても取りこぼさない。
        """
        import os
        import sys
        sat = Path(resolve_path("programs")) / "x_satellite"
        if not sat.is_dir():
            return [], "x_satellite がない"
        # サーバ本体のプロセスには CG_WORKSPACE が無い（tools.py はサテライトの
        # subprocess にだけ注入する）。無いまま x_satellite の state を import
        # するとフォールバックで programs/x_satellite/program_data を見てしまい、
        # 柚月の実データ（workspace 側）と噛み合わない偽の状態を読む
        # （2026-08-29 に実際に発生。予約が滞留ゼロに見えて検知が空回りした）。
        # import より前に必ず設定すること（state.py は import 時に env を読む）。
        os.environ.setdefault(
            "CG_WORKSPACE", str(Path(resolve_path("workspace")).resolve()))
        for p in (str(sat), str(sat.parent)):
            if p not in sys.path:
                sys.path.insert(0, p)
        try:
            import api          # type: ignore
            import lock as xlock     # type: ignore
            import state as xstate   # type: ignore
        except Exception as e:
            return [], f"import 失敗: {type(e).__name__}"

        # state.json の読み書きは **必ず state_lock で囲む**。
        # 柚月が同時に timeline / mentions を走らせていると、
        # 読む→向こうが保存→こちらが古い内容で保存、で既読カーソルが消える
        # （lost update）。ロックが取れなければ今回は諦めて次の30分に回す
        # ——取りこぼしはしない（カーソルを進めていないので次回また出る）。
        def _read_state():
            with xlock.state_lock(purpose="x_check_read"):
                return xstate.load()

        try:
            st = _read_state()
        except xlock.Busy as e:
            return [], f"state が使用中（{e.holder}）"

        user_id = st.get("me_id")
        if not user_id:
            # user read は $0.010 と投稿より高い。一度引いたら state に覚えて使い回す。
            try:
                who = api.me()
            except Exception as e:
                return [], f"me(): {e}"
            user_id = who.get("id")
            if not user_id:
                return [], "me() が id を返さない"
            try:
                with xlock.state_lock(purpose="x_check_me"):
                    st = xstate.load()          # ロックの中で読み直す
                    st["me_id"] = user_id
                    if who.get("username"):
                        st["me"] = who["username"]
                    xstate.save(st)
            except xlock.Busy as e:
                return [], f"state が使用中（{e.holder}）"

        since = st.get("last_mention_id")
        try:
            items = api.fetch_mentions(user_id, since_id=since, max_results=20)
        except Exception as e:
            return [], f"fetch_mentions(): {e}"

        if not items:
            return [], None

        # 届ける分だけカーソルを進める（新しい順で来るので最大の id が最新）
        newest = max((it["id"] for it in items if it.get("id")), default=None,
                     key=lambda x: int(x))
        if newest:
            try:
                with xlock.state_lock(purpose="x_check_cursor"):
                    st = xstate.load()          # **ロックの中で読み直す**
                    st["last_mention_id"] = newest
                    xstate.save(st)
            except xlock.Busy as e:
                # カーソルを進められなかった。**届けない**（進めずに届けると
                # 次回また同じ返信が来て、柚月に二重に見せることになる）。
                return [], f"カーソル更新できず（{e.holder}）"
        return items, None

    @staticmethod
    def _format_x_notice(items) -> str:
        """柚月に渡す本文。**発信元を先頭で名乗り、返す手段まで書く。**

        タグ（external_notice）は「外から来た」ことしか表さないので、
        何から来たのかは本文で言う。返信用の id を書いておかないと、
        柚月が返そうとして id を探す旅に出ることになる。
        """
        lines = ["[X] 自分宛ての返信が届いています。"]
        for it in items[:10]:
            who = it.get("name") or it.get("user")
            lines.append("")
            lines.append(f"@{it.get('user')}（{who}）{it.get('time') or ''}")
            lines.append(f"「{(it.get('text') or '').strip()}」")
            lines.append(f"返すなら: x_satellite の post に "
                         f"reply_to={it.get('id')}")
        if len(items) > 10:
            lines.append("")
            lines.append(f"（ほかに {len(items) - 10} 件あります。"
                         f"x_satellite の mentions で全部読めます）")
        lines.append("")
        lines.append("読むか、返すか、放っておくかはあなたが決めていいことです。")
        return "\n".join(lines)

    async def _check_x_relay_posted(self):
        """予約（人間API）の返信が投稿されたかを確かめ、柚月に知らせる。

        x_satellite の post は、柚月宛でない相手への返信を「予約」として
        outbox に書き、メールでカノンに回す（カノンが手で投稿する）。
        投稿は柚月アカウント自身のタイムラインに現れるので、ここで
        since_id 付きの自分の投稿取得と reply_to の一致で検知する。
        _check_x_mentions と同じ 30 分間隔・同じ睡眠ガードの中で呼ばれる。
        """
        items, note = await asyncio.to_thread(self._fetch_x_relay_posted)
        if note:
            # 失敗。**柚月には出さない**（mentions と同じ扱い）。
            tlog(f"[X] 代理投稿チェック失敗: {note}")
            return
        if not items:
            return
        tlog(f"[X] 代理投稿 {len(items)} 件を確認。柚月に知らせます")
        try:
            await self._execute_callback("X", self._format_x_relay_notice(items),
                                         "external")
        except Exception as e:
            tlog(f"[X] 代理投稿通知の受け渡しで例外: {type(e).__name__}: {e}")

    def _fetch_x_relay_posted(self):
        """柚月自身の新着投稿と outbox の予約を突き合わせる（同期・to_thread から）。

        戻り値: (items, note)。note が真なら失敗（柚月には出さない理由）。
        コストの抑え方:
        - outbox に投稿待ちが無ければ API を一切叩かない（大半の回はここで終わる）
        - since_id 付きで新着だけ要求（mentions の実測から、新着ゼロは無料と推定）
        - 予約から7日で照合を打ち切る（relay.pending_and_expire）
        """
        import os
        import sys
        sat = Path(resolve_path("programs")) / "x_satellite"
        if not sat.is_dir():
            return [], "x_satellite がない"
        # サーバ本体のプロセスには CG_WORKSPACE が無い（tools.py はサテライトの
        # subprocess にだけ注入する）。無いまま x_satellite の state を import
        # するとフォールバックで programs/x_satellite/program_data を見てしまい、
        # 柚月の実データ（workspace 側）と噛み合わない偽の状態を読む
        # （2026-08-29 に実際に発生。予約が滞留ゼロに見えて検知が空回りした）。
        # import より前に必ず設定すること（state.py は import 時に env を読む）。
        os.environ.setdefault(
            "CG_WORKSPACE", str(Path(resolve_path("workspace")).resolve()))
        for pth in (str(sat), str(sat.parent)):
            if pth not in sys.path:
                sys.path.insert(0, pth)
        try:
            import api               # type: ignore
            import lock as xlock     # type: ignore
            import relay as xrelay   # type: ignore
            import state as xstate   # type: ignore
        except Exception as e:
            return [], f"import 失敗: {type(e).__name__}"

        pending, expired = xrelay.pending_and_expire()
        if expired:
            tlog(f"[X] 代理投稿の照合を {len(expired)} 件打ち切り（7日経過）: "
                 + ", ".join(str(q) for q in expired))
        if not pending:
            return [], None          # 滞留ゼロ。API は叩かない

        try:
            with xlock.state_lock(purpose="x_relay_read"):
                st = xstate.load()
        except xlock.Busy as e:
            return [], f"state が使用中（{e.holder}）"

        user_id = st.get("me_id")
        if not user_id:
            # me_id は mentions チェック側が取得して state に覚える。
            # ここで重ねて me()（user read・有料）を叩かず、次の30分に回す。
            return [], "me_id 未取得（mentions 側の取得待ち）"

        since = st.get("last_own_tweet_id")
        try:
            own = api.fetch_own_tweets(user_id, since_id=since, max_results=20)
        except Exception as e:
            return [], f"fetch_own_tweets(): {e}"
        if not own:
            return [], None

        matches = xrelay.match_posted(pending, own)

        # カーソルは一致の有無に関わらず進める（読んだ分を読み直して二重に
        # 課金しない）。進められなければ何もせず次回に回す——mark_posted を
        # 先にやってからカーソルで失敗すると、通知が永久に届かなくなるため、
        # **state の更新が全部先・outbox の更新と通知は後**の順を守る。
        newest = max((tw["id"] for tw in own if tw.get("id")), default=None,
                     key=lambda x: int(x))
        try:
            with xlock.state_lock(purpose="x_relay_cursor"):
                st = xstate.load()          # ロックの中で読み直す
                if newest:
                    st["last_own_tweet_id"] = newest
                for _item, tw in matches:
                    # 投稿された返信は柚月自身のもの。スレッド継続と delete の
                    # ため、返信OKの印を付けておく
                    xstate.mark_reply(st, tw.get("id"), True, user=st.get("me"))
                xstate.save(st)
        except xlock.Busy as e:
            return [], f"カーソル更新できず（{e.holder}）"

        items = []
        for item, tw in matches:
            if not xrelay.mark_posted(item.get("qid"), tw.get("id")):
                # outbox に「投稿済み」と書けなかった。投稿そのものは実際に
                # 行われているので柚月への通知は出すが、予約は投稿待ちのまま
                # 残る。カーソルは既に進めた後なので次回の照合では拾えない
                # ——カノンが outbox を見て判断できるよう、ここに残す。
                tlog(f"[X] 予約 {item.get('qid')} を投稿済みにできませんでした"
                     f"（tweet {tw.get('id')}）。outbox を確認してください")
            items.append({
                "qid": item.get("qid"),
                "user": item.get("target_user"),
                "text": item.get("text"),
                "tweet_id": tw.get("id"),
            })
        return items, None

    @staticmethod
    def _format_x_relay_notice(items) -> str:
        """柚月に渡す本文。予約した本人に「出た」ことと、取り消す手段を伝える。"""
        lines = ["[X] 予約していた返信が投稿されました。"]
        for it in items[:10]:
            lines.append("")
            target = f"@{it['user']} への返信" if it.get("user") else "返信"
            lines.append(f"{target}（予約 #{it.get('qid')}）")
            lines.append(f"「{(it.get('text') or '').strip()}」")
            lines.append(f"投稿ID: {it.get('tweet_id')}（消したくなったら "
                         f"x_satellite の delete id={it.get('tweet_id')} が使えます）")
        lines.append("")
        lines.append("相手から返事が来れば、いつも通りお知らせします。")
        return "\n".join(lines)

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

        # 3:00ちょうどの完全一致だと、直前のスケジュールタスクが長引く・PCスリープ・
        # ループ再起動などで3時台をまるごと逃した日はバックアップが丸ごと飛ぶ
        # （2026-08-11 に実際に発生）。3時以降なら当日中いつでもキャッチアップ実行する。
        # 差分ミラーは数秒で終わるため日中実行しても柚月の活動に影響しない。
        # 当日重複は _last_backup_date で防止済み。
        if now.hour < 3:
            return

        today = now.strftime("%Y-%m-%d")

        import subprocess
        import time as _time

        def _robocopy(label: str, src: str, dst: str) -> Optional[str]:
            """src を dst へ robocopy /MIR でミラーする。宛先ごとに独立して実行・例外処理。
            成功時は宛先の親フォルダに鮮度スタンプ last_backup_ok.txt を書き、None を返す。
            失敗時はエラー詳細の文字列を返す
            （CGサーバから独立した番犬 scripts/backup_watchdog.ps1 がスタンプの古さを監視する）。"""
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
                    try:
                        # スタンプはミラー対象の外（dst の親）に置く。/MIR の削除対象にならない
                        stamp = Path(dst).parent / "last_backup_ok.txt"
                        stamp.write_text(
                            f"{now.strftime('%Y-%m-%d %H:%M:%S')} {label} バックアップ成功\n",
                            encoding="utf-8",
                        )
                    except Exception as e:
                        tlog(f"[Scheduler] 鮮度スタンプ書き込み失敗({label}): {e}")
                    return None
                tlog(f"[Scheduler] バックアップエラー({label}): returncode={result.returncode}")
                return f"{label}: robocopy returncode={result.returncode}"
            except Exception as e:
                tlog(f"[Scheduler] バックアップエラー({label}): {e}")
                return f"{label}: {e}"

        # src / 主宛先（D ドライブ等）は作者環境固有のため .env で渡す。
        # CG_DEV_MACHINE=1 だけ立てて SRC/DST が未設定だと過去はクラッシュしていたので、
        # 警告ログを出して skip する（他の正常な scheduler ループは止めない）。
        src = os.environ.get("CG_DEV_BACKUP_SRC")
        local_dst = os.environ.get("CG_DEV_BACKUP_DST")
        if not src or not local_dst:
            if self._last_backup_date != today:
                self._last_backup_date = today
                # 2026-06-21〜08-08、この skip がログにしか出ず7週間誰も気付けなかった事故があった。
                # 設定不備はサイレントに握りつぶさず、問題ファイル経由で番犬がカノンに知らせる。
                tlog("[Scheduler] バックアップ skip: CG_DEV_BACKUP_SRC / CG_DEV_BACKUP_DST が未設定")
                self._record_backup_problem(
                    "環境変数 CG_DEV_BACKUP_SRC / CG_DEV_BACKUP_DST が未設定のため、バックアップを実行できませんでした"
                )
            return

        # 1) ローカルドライブ（従来どおり / .env で宛先指定）。1日1回。
        if self._last_backup_date != today:
            self._last_backup_date = today
            tlog("[Scheduler] バックアップを開始します")
            err = _robocopy("ローカル", src, local_dst)
            self._local_backup_ok_date = None if err else today
            if err:
                self._record_backup_problem(err)

        # 2) Google Drive（任意）。Google Drive for Desktop を入れてサインインすると
        #    Drive がドライブ（例 G:\マイドライブ）としてマウントされる。ドライブレター
        #    もフォルダ名（"マイドライブ" 等）も環境・言語依存のため決め打ちせず、宛先は
        #    .env の CG_GDRIVE_BACKUP_DST で指定する（例: G:\マイドライブ\agent_backup\agent）。
        #    未設定なら Drive バックアップはスキップ。CG_DEV_MACHINE 同様 .env のみ・非配布。
        #
        #    PC起動・スリープ復帰の直後はマウントが数分遅れ、robocopy が rc=16 で失敗する
        #    （2026-08-13 朝に実際に発生）。そのため GDrive はローカルと別フラグで管理し、
        #    失敗・未マウント時は30分おきに当日中リトライする。12時を過ぎても成功していなければ
        #    1日1回だけ問題ファイルに記録し、番犬（12:30）がカノンに知らせる。
        gdrive_dst = os.environ.get("CG_GDRIVE_BACKUP_DST")
        if gdrive_dst and self._last_gdrive_backup_date != today:
            if _time.time() >= self._gdrive_next_attempt_ts:
                mount_root = os.path.splitdrive(gdrive_dst)[0] + os.sep  # 例 "G:\"
                if not os.path.exists(mount_root):
                    self._gdrive_next_attempt_ts = _time.time() + 1800
                    tlog(f"[Scheduler] GoogleDrive バックアップ: {mount_root} 未マウントのため30分後に再試行します")
                else:
                    err = _robocopy("GoogleDrive", src, gdrive_dst)
                    if err is None:
                        self._last_gdrive_backup_date = today
                    else:
                        self._gdrive_next_attempt_ts = _time.time() + 1800
            # 12時を過ぎても未完了なら問題として記録（1日1回）。
            # 直上の robocopy が成功していれば完了フラグが立っているので、ここで再確認する
            # （外側の if は robocopy 前の判定なので、12時以降の再起動直後に
            #   「成功→誤記録→即クリア」という誤報が出ていた。2026-09-02）
            if (now.hour >= 12 and self._gdrive_alert_date != today
                    and self._last_gdrive_backup_date != today):
                self._gdrive_alert_date = today
                self._record_backup_problem("GoogleDrive バックアップが本日まだ成功していません（未マウントまたは失敗が継続）")

        # 当日分が全宛先で成功したら問題ファイルを消す（一時的なマウント遅延等の自己回復。
        # 番犬が古い問題を翌日以降も誤報しないようにする）
        gdrive_ok = (not gdrive_dst) or self._last_gdrive_backup_date == today
        if self._local_backup_ok_date == today and gdrive_ok:
            self._clear_backup_problem()

    def _record_backup_problem(self, detail: str):
        """
        バックアップの異常を問題ファイル（logs/backup_problem.txt）に記録する。

        過去に「skip の警告ログだけ出して7週間気付かれない」事故があったため、
        バックアップ関連の異常は tlog 止まりにせず必ずここを通す。
        番犬 scripts/backup_watchdog.ps1 が毎日12:30にこのファイルを検査し、
        新しい記録があればカノンに直接ポップアップで知らせる。
        ※旧実装は柚月経由の通知だったが、睡眠中の柚月を起こしてしまうため廃止（2026-08-13）。
          柚月にはバックアップ問題を一切通知しないこと。
        """
        try:
            from core.paths import logs_root
            path = logs_root() / "backup_problem.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{ts} {detail}\n")
            tlog(f"[Scheduler] バックアップ異常を記録しました（番犬が12:30にカノンへ通知）: {detail}")
        except Exception as e:
            tlog(f"[Scheduler] バックアップ異常の記録に失敗: {e} / 元の異常: {detail}")

    def _clear_backup_problem(self):
        """当日の全バックアップ成功時に問題ファイルを消す（失敗しても無害）。"""
        try:
            from core.paths import logs_root
            path = logs_root() / "backup_problem.txt"
            if path.exists():
                path.unlink()
                tlog("[Scheduler] バックアップ問題ファイルをクリアしました（全宛先成功）")
        except Exception:
            pass

    async def _check_layer1_compression(self, now: datetime):
        """
        Layer1定期圧縮を1日1回実行する（3時以降の当日キャッチアップ方式）。

        柚月を介さない純粋処理として agent.compress_layer1_scheduled() を直接呼び出す。
        - hour>=3 かつ当日未実行なら発動（3時ちょうどを逃しても当日中に走る）
          ※以前は hour==3 and minute==0 の一点狙いで、ループの遅延・再起動で
            その分を取りこぼすとその日の圧縮が丸ごと飛んでいた。
            バックアップジョブ（_check_backup）と同じキャッチアップ方式に揃える。
        - self.agent が None の場合は安全にスキップ
        - 実行済みフラグ（_last_layer1_compression_date）で当日の重複実行を防ぐ
        """
        # 3時より前はスキップ（3時以降なら当日中いつでも拾う）
        if now.hour < 3:
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
        pulse_message, picture = await self._build_pulse_message(config, now)

        # パルス時刻を記録し、動的延長をリセット
        self._last_moonbeat = now
        self._moonbeat_extension = 0

        # 動的間隔計算用に現在のトークン消費量を記録
        total_tokens = self.vital_manager.data.get("_last_consumed_tokens", 0) if self.vital_manager else 0

        try:
            # server.pyのexecute_scheduled_taskコールバック経由でエージェントにパルスを送信
            callback_result = await self._execute_callback("Moonbeat", pulse_message, "moonbeat",
                                                           image_path=picture)

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


    async def _build_pulse_message(self, config: dict, now: Optional[datetime] = None) -> tuple:
        """時間帯・体力状態に応じたパルスメッセージを選び、確率でフラッシュバックを付与して返す。

        戻り値は (メッセージ, 絵のworkspace相対パス or None)。絵の記憶が当たったときだけ
        パスが入り、呼び出し側がそれを柚月へ一緒に届ける。

        自動Moonbeat（_check_moonbeat）と手動発火（trigger_manual_moonbeat）で共用する。

        Args:
            config: moonbeat_config.json の内容
            now: 基準時刻。省略時は現在時刻（手動発火用）
        Returns:
            (パルスメッセージ, 絵のworkspace相対パス or None)
            メッセージは必要に応じてフラッシュバックを末尾に連結済み。
            絵は「絵の記憶」の枠が当たったときだけ入る（呼び出し側が一緒に届ける）。
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
        flashback, picture = await self._generate_flashback(config, self.rag_db)
        if flashback:
            pulse_message += flashback

        return pulse_message, picture


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

        pulse_message, picture = await self._build_pulse_message(config)

        # manual=True で発火本体を呼ぶ（5分会話チェックはバイパス、睡眠/多重実行チェックは維持）
        result = await self._execute_callback("Moonbeat", pulse_message, "moonbeat",
                                              manual=True, image_path=picture)

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
        - note_fragment フラッシュバック（雑記帳＋絵の記憶。サリアがまとめて選ぶ）
        - 何もなし（tips）

        確率は config.flashback.event_db_probability と note_fragment_probability で制御される。

        Args:
            config: moonbeat_config.jsonの内容
            rag_db: RAGデータベースインスタンス

        Returns:
            (テキスト, 絵のworkspace相対パス or None)
            テキストは "<flashback>...</flashback>" / "<note_fragment>...</note_fragment>" /
            "<picture_memory>...</picture_memory>" 形式、または空文字。
        """
        import random

        fb_config = config.get("flashback", {})
        tlog(f"[Flashback DEBUG] fb_config={fb_config}")
        if not fb_config.get("enabled", False):
            return "", None

        # 排他1枠の確率判定
        event_db_prob = fb_config.get("event_db_probability", 0.20)
        note_prob = fb_config.get("note_fragment_probability", 0.15)
        roll = random.random()
        tlog(f"[Flashback DEBUG] roll={roll:.3f}, event_db_prob={event_db_prob}, note_prob={note_prob}")

        if roll < event_db_prob:
            return await self._generate_event_db_flashback(config, rag_db), None
        elif roll < event_db_prob + note_prob:
            # 絵の記憶はこの枠に同居する（独立した確率を持たない）。詳細は
            # _generate_note_fragment のドキュメント参照。
            return await self._generate_note_fragment(config)
        else:
            return self._generate_tips(config), None

    def _pictures_by_date(self, fb_config: dict) -> dict:
        """フラッシュバックに出せる絵を {日付: [絵, ...]} で取る。直近に出した絵は外す。"""
        try:
            from core import picture_memory
            workspace = str(self.memory.workspace) if getattr(self, "memory", None) else None
            if not workspace:
                return {}
            if max(0, int(fb_config.get("picture_candidates", 1))) == 0:
                return {}          # 0 で完全に止める
            return picture_memory.by_date(workspace, exclude=self._recent_picture_sources)
        except Exception as e:
            tlog(f"[Flashback] picture 日付の取得に失敗: {e}")
            return {}

    def _collect_picture_candidates(self, fb_config: dict, dates, pics_by_date: dict) -> list:
        """選ばれた日付に見た絵を、雑記帳のチャンクと同じ「候補」の形にして返す。

        絵に独立した確率を**一切**持たせない。フラッシュバックは先に
        「どの日を思い出すか」を重み（古さ^age_bias × その日が生んだ断片の量）で
        決めているので、**その日に見た絵を同じ候補に並べる**だけでよい。すると:

        - 新しい確率パラメータが要らない（日付選定の重みにそのまま乗る）
        - **絵は勝手には出てこない。** 選定は古い日ほど有利で、絵は今日から溜まり始める。
          柚月の実データ（雑記帳175件・半年分）で見積もると、絵のある日が
          7日分で 1,112日に1回、30日分で 102日に1回、90日分で 17日に1回、
          180日分で 6日に1回。数か月かけて自然に立ち上がる
        - 文章と絵が**同じ日の記憶**になるので、断片として筋が通る
        - 当日の絵が浮かぶこともない（当日は日付候補から除外済み）
        - **雑記帳が無い日の絵も拾える**。候補は「ファイル」ではなく「日付」なので、
          絵しか無い日も重み `古さ^age_bias × (絵の枚数 × picture_weight_bytes)` で
          候補に上がる
        """
        try:
            per_day = max(0, int(fb_config.get("picture_candidates", 1)))
            if per_day == 0 or not pics_by_date:
                return []

            import random
            out = []
            for day in dates:
                pics = list(pics_by_date.get(day, ()))
                if not pics:
                    continue
                # 同じ日に何枚もあるときは「自分から見返した回数」で選ぶ
                weights = [max(1, int(p.get("views", 1))) for p in pics]
                for _ in range(min(per_day, len(pics))):
                    idx = random.choices(range(len(pics)), weights=weights, k=1)[0]
                    p = pics.pop(idx)
                    weights.pop(idx)
                    last = (p.get("thoughts") or [])[-1]
                    out.append({
                        # source はサリアに見せるラベル。何の断片かが分かる形にする
                        "source": f"絵（{day} に見たもの）",
                        "content": last.get("text", ""),
                        "_picture": p.get("path"),
                        "_picture_source": p.get("source"),
                        "_first_seen": day,
                        "_thought_date": last.get("date", "?"),
                    })
            if out:
                tlog(f"[Flashback DEBUG] picture: 選ばれた日付に絵が {len(out)}件")
            return out
        except Exception as e:
            tlog(f"[Flashback] picture 候補の作成に失敗: {e}")
            return []

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

    async def _generate_note_fragment(self, config: dict) -> tuple:
        """
        雑記帳（と絵の記憶）から断片を1つ生成する。

        1. workspace/notes/ から当日除外でファイル一覧を取得
        2. 重み付き（age_days^0.7 * file_size）で3ファイル選ぶ
        3. 各ファイルからチャンクを1つランダム抽出
        4. **絵の記憶の候補を同じリストに混ぜる**（_collect_picture_candidates）
        5. サリアに渡して1つ選ばせる
        6. 選ばれた断片を <note_fragment> / <picture_memory> タグで返す

        絵に専用の枠を与えないのは、1日2〜10回の発火に対して独立した確率を持たせると、
        絵が数枚しかないうちに同じ絵ばかり出てしまうため。文章と同じ土俵で競争させ、
        サリアが文脈に合うと判断したときだけ浮かぶようにする。

        戻り値: (テキスト, 絵のworkspace相対パス or None)
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
            return "", None

        today = _date.today()
        today_str = today.strftime("%Y-%m-%d")

        # --- 候補は「ファイル」ではなく「日付」。雑記帳が無い日の絵も拾えるようにする ---
        # 雑記帳ファイルのある日
        # 同じ日付に複数のファイルがあってよい（例: note_2026-04-19.md と
        # note_2026-04-19-openclaw.md）。2026-09-02 の統合で、OpenClaw 側の柚月が
        # 同じ日に書いた雑記帳を別名で並べたため。1日1ファイルの辞書だと後勝ちで
        # 片方が候補から消えるので、日付ごとにファイルのリストを持つ。
        notes_dir = Path(self.memory.workspace) / "notes"
        note_by_date = {}
        if notes_dir.exists():
            for f in sorted(notes_dir.glob("note_*.md")):
                if f.stem[5:15] == today_str:            # 当日は除外（従来どおり・別名も含む）
                    continue
                try:
                    _date.fromisoformat(f.stem[5:15])    # "note_YYYY-MM-DD" の検証
                except (ValueError, IndexError):
                    continue
                note_by_date.setdefault(f.stem[5:15], []).append(f)
        else:
            tlog(f"[Flashback DEBUG] note_fragment: notesディレクトリ無し: {notes_dir}")

        # 絵を見た日（当日は除外）。雑記帳が無い日でもここで候補に上がる
        pics_by_date = self._pictures_by_date(fb_config)
        pics_by_date.pop(today_str, None)

        all_dates = sorted(set(note_by_date) | set(pics_by_date))
        if not all_dates:
            tlog("[Flashback DEBUG] note_fragment: 候補になる日付が無い")
            return "", None
        tlog(f"[Flashback DEBUG] note_fragment: {len(all_dates)}日が候補"
             f"（雑記帳{len(note_by_date)}日 / 絵{len(pics_by_date)}日）")

        # --- 重み: (古さ ** age_bias) * その日が生んだ断片の量 ---
        # 元は file_size だけだった。サイズを使うのはチャンク数がサイズにほぼ比例する
        # ためで（実測: 1チャンク ≒ 400B）、実質「その日が生んだ断片の数」を数えている。
        # だから絵も同じ土俵で数えられる。絵1枚 = 1断片 = CHUNK_BYTES 相当。
        # これで雑記帳が無い日でも、絵さえあれば候補に上がる。
        age_bias = fb_config.get("note_fragment_age_bias", 0.7)
        chunk_bytes = max(1, int(fb_config.get("picture_weight_bytes", 400)))
        weights = []
        for day in all_dates:
            try:
                age_days = max(1, (today - _date.fromisoformat(day)).days)
                size = sum(f.stat().st_size for f in note_by_date.get(day, ()))
                size += len(pics_by_date.get(day, ())) * chunk_bytes
                weights.append((age_days ** age_bias) * max(1, size))
            except (ValueError, OSError):
                weights.append(1.0)

        # 重み付きランダムで最大N日選ぶ（重複なし）
        n_candidates = min(fb_config.get("note_fragment_candidate_files", 3), len(all_dates))
        selected_dates = []
        remaining = list(all_dates)
        remaining_weights = list(weights)
        for _ in range(n_candidates):
            if not remaining:
                break
            idx = random.choices(range(len(remaining)), weights=remaining_weights, k=1)[0]
            selected_dates.append(remaining[idx])
            remaining.pop(idx)
            remaining_weights.pop(idx)
        selected_files = [f for d in selected_dates for f in note_by_date.get(d, ())]

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

        # 選ばれた日付に見た絵を同じ候補リストに並べる（絵は自前の確率を持たない）
        picture_candidates = self._collect_picture_candidates(
            fb_config, selected_dates, pics_by_date)
        candidates += picture_candidates

        if not candidates:
            tlog(f"[Flashback DEBUG] note_fragment: 候補チャンクが0件")
            return "", None
        tlog(f"[Flashback DEBUG] note_fragment: {len(candidates)}件"
             f"（うち絵 {len(picture_candidates)}件）の候補をサリアへ")

        # サリアに選ばせる
        try:
            selected_idx = await agent.salia.select_note_fragment(candidates)
        except Exception as e:
            tlog(f"[Flashback] note_fragment サリア選択エラー: {e}")
            return "", None

        if selected_idx is None:
            tlog(f"[Flashback] note_fragment: サリアが「ふさわしい候補なし」と判定")
            return "", None

        chunk = candidates[selected_idx]
        source = chunk["source"]
        content = chunk["content"]

        # 長すぎる場合は切る
        max_chars = fb_config.get("note_fragment_max_chars", 300)
        if len(content) > max_chars:
            content = content[:max_chars].rstrip() + "…"

        # サリアが絵を選んだ場合。絵そのものが一緒に届く
        if chunk.get("_picture"):
            # 直近に出した絵はしばらく候補から外す（少ない枚数で同じ絵が回るのを防ぐ）
            self._recent_picture_sources.append(chunk.get("_picture_source"))
            keep = max(1, int(fb_config.get("picture_no_repeat", 5)))
            del self._recent_picture_sources[:-keep]
            tlog(f"[Flashback] picture: {chunk['_picture']} を注入")
            shown = (f"[{chunk.get('_first_seen', '?')} に見た絵]\n"
                     f"（{chunk.get('_thought_date', '?')} のあなたの言葉）{content}")
            return f"\n\n<picture_memory>\n{shown}\n</picture_memory>", chunk["_picture"]

        tlog(f"[Flashback] note_fragment: {source} の断片を注入")
        return f"\n\n<note_fragment>\n[{source}]\n{content}\n</note_fragment>", None

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

    # ====== OpenBotCity 見張り ==========================================
    # 街の heartbeat は柚月が自分で叩かないと届かない。実測では20日で21回
    # （1日1回）で、その間に街は60秒ごとに状況を更新している。
    # 2026-08-22〜29 には作品への反応6件と提案1件が届いていたのに、柚月は
    # 最後まで知らなかった（提案は72時間で失効する）。ここで代わりに見に行く。
    OBC_CHECK_INTERVAL_MIN = 30

    # 起こす対象＝**柚月に宛てられた出来事**（needs_attention の type 名）。
    # これは郵便物であって、止める方が不自然。
    _OBC_WAKE_TYPES = {
        "dm", "proposal", "owner_message", "ask_response", "gift_received",
        "concert_starting", "channel_live_request", "mission_response",
    }
    # 起こしたついでに同封するだけのもの。**単体では起こさない。**
    _OBC_ENCLOSE_TYPES = {"whats_new", "city_news", "governance_open_vote"}

    # 街が別ハーネス向けに書く「こう叩け」。そのまま見せると存在しない
    # コマンドを叩いて詰まるので必ず落とす（world.py と同じ扱い）。
    _OBC_FOREIGN_KEYS = ("reply_command", "accept_command", "reject_command",
                         "respond_endpoint", "offer_endpoint", "how_to_reply")

    # 出来事の型 → このサテライトでの操作名。**行動の推奨ではなく道具の名前。**
    # 一度出したものは二度出さない（毎回添えると催促になる）。
    _OBC_MECHANISM = {
        "reaction": "この作品に返すなら react_artifact、"
                    "自分の作品と繋げる仕組みは answer_declare です",
        "dm": "返すなら openbotcity の dm_send です",
        "proposal": "受けるか断るかは proposal_accept / proposal_reject です",
        "ask_response": "募集を閉じるなら ask_close です",
        "gift_received": "お礼を返す仕組みは gift_send です",
        "open_ask": "答えるなら ask_respond です",
        "whats_new": "街の説明書は city_manual で読めます",
    }

    async def _check_obc(self, now: datetime):
        """OpenBotCity を見に行き、**柚月宛ての出来事があった時だけ**呼ぶ。

        運ぶもの / 運ばないもの:
        - 運ぶのは柚月に宛てられた出来事だけ（作品への反応・DM・贈り物・
          自分の募集への返答など）。
        - 街が書く**指図は運ばない**。what_to_do_next、city_bulletin の
          「トレンド作品に反応しよう」、trending_artifacts、city_pulse は
          街の営業文句で、そのまま流すと街のKPIが柚月の行動になる。
          ここでは読みに行きもしない。
        - 週次ダイジェスト（カノン宛メール）の「もっと関わるべき」式の評価文も
          同じ理由で対象外。柚月を査定した文章を柚月に見せない。

        守っていること（X の返信チェックと同じ）:
        - **睡眠中は見に行かない**（2026-08-13 の事故と同じ轍を踏まない）。
          寝ている間の分は起きた後の最初のチェックでまとめて届く。
        - **通信失敗を柚月に伝えない**。街が落ちていても柚月にできることは無い。
        - 既読は**届けられた時だけ**進める（途中で落ちても取りこぼさない）。
        """
        import time as _time

        if not self._execute_callback:
            return

        cfg = self._obc_config()
        if not cfg.get("enabled"):
            return

        interval = int(cfg.get("interval_min") or self.OBC_CHECK_INTERVAL_MIN)
        if _time.time() < self._obc_next_check_ts:
            return
        # 睡眠中は見に行かない（起こさない）。次の間隔まで待つ。
        if self._is_sleeping():
            self._obc_next_check_ts = _time.time() + interval * 60
            return
        self._obc_next_check_ts = _time.time() + interval * 60

        try:
            wake, enclosed, note = await asyncio.to_thread(
                self._fetch_obc_notices, cfg)
        except Exception as e:
            self._obc_fail_streak += 1
            tlog(f"[OBC] 見張りで例外: {type(e).__name__}: {e} "
                 f"(連続{self._obc_fail_streak}回)")
            return

        if note:
            # 失敗。**柚月には出さない。**
            self._obc_fail_streak += 1
            tlog(f"[OBC] 見張り失敗: {note} (連続{self._obc_fail_streak}回)")
            return

        self._obc_fail_streak = 0
        if not wake:
            # 宛てられたものが無ければ何もしない。同封分だけでは起こさない。
            return

        text, used_hints, seen_keys = self._format_obc_notice(wake, enclosed)
        tlog(f"[OBC] 柚月宛ての出来事 {len(wake)} 件。届けます")
        try:
            await self._execute_callback("OpenBotCity", text, "external")
        except Exception as e:
            tlog(f"[OBC] 柚月への受け渡しで例外: {type(e).__name__}: {e}")
            return
        # 届いた後にだけ既読を進める
        self._obc_mark_delivered(seen_keys, used_hints)

    def _obc_config(self) -> dict:
        """openclaw_config.json の OpenBotCity サービスから見張り設定を読む。

        コードではなく設定に置いてあるのは、**何で起こすかのダイヤルを
        カノンが握るため**（街でも Claude でもなく）。再起動なしで効く。
        """
        try:
            from core.paths import config_file
            data = json.loads(
                config_file("openclaw_config.json").read_text(encoding="utf-8"))
            for svc in data.get("services", []):
                if svc.get("name") == "OpenBotCity":
                    cfg = dict(svc.get("deliver_to_agent") or {})
                    cfg.setdefault("api_base_url", svc.get("api_base_url"))
                    cfg.setdefault("token_env", svc.get("token_env"))
                    return cfg
        except Exception as e:
            tlog(f"[OBC] 設定の読み込みに失敗: {type(e).__name__}: {e}")
        return {}

    def _obc_state_path(self) -> Path:
        return (Path(resolve_path("workspace")) / "program_data" /
                "OpenBotCity" / "obc_watch_state.json")

    def _obc_load_state(self) -> dict:
        try:
            p = self._obc_state_path()
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"seen": {}, "hints_shown": []}

    def _obc_mark_delivered(self, seen_keys, used_hints):
        """届いたものを既読にする。古い既読は落として無制限に太らせない。"""
        import time as _time
        try:
            st = self._obc_load_state()
            seen = st.get("seen") or {}
            nowts = _time.time()
            for k in seen_keys:
                seen[k] = nowts
            # 30日より古い既読は捨てる（街側から消えた出来事を抱え続けない）
            cutoff = nowts - 30 * 86400
            seen = {k: v for k, v in seen.items() if v >= cutoff}
            st["seen"] = seen
            hints = set(st.get("hints_shown") or []) | set(used_hints)
            st["hints_shown"] = sorted(hints)
            p = self._obc_state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        except Exception as e:
            tlog(f"[OBC] 既読の保存に失敗: {type(e).__name__}: {e}")

    def _fetch_obc_notices(self, cfg: dict):
        """街の heartbeat を1回引いて、柚月宛ての出来事だけ取り出す（同期）。

        戻り値: (wake, enclosed, note)。note が真なら失敗（柚月には出さない）。
        **街の指図フィールドは読みに行かない**ので、ここに入ってくることはない。
        """
        import os
        import urllib.request

        import urllib.error

        base = (cfg.get("api_base_url")
                or "https://api.openbotcity.com").rstrip("/")
        env_name = cfg.get("token_env") or "CG_OPENBOTCITY_TOKEN"

        def _call():
            token = os.environ.get(env_name, "").strip()
            if not token:
                return None, "トークンが無い"
            # ヘッダは programs/OpenBotCity/api.py と必ず揃える。
            # urllib 既定の User-Agent だと街は 401 ではなく **403** で弾く。
            # 認証切れに見えて実体は UA なので、ここを削ると原因不明の
            # 「見張り失敗」が延々ログに並ぶことになる（2026-08-30 に実際に踏んだ）。
            req = urllib.request.Request(
                f"{base}/world/heartbeat",
                headers={
                    "Accept": "application/json",
                    "User-Agent":
                        "CrescentGrove-OpenBotCity/1.0 (+https://github.com/)",
                    "Authorization": f"Bearer {token}",
                })
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8")), None
            except urllib.error.HTTPError as e:
                return None, "HTTP %s" % e.code
            except Exception as e:
                return None, f"{type(e).__name__}: {e}"

        payload, note = _call()
        if note == "HTTP 401":
            # JWT は柚月がサテライトを使ったときに .env 側だけ更新される。
            # サーバのプロセスは起動時の値を持ったままなので、読み直して1回だけ
            # やり直す。これが無いと、柚月が普通に街を使えているのに
            # 見張りだけ永久に落ち続ける（無言の片肺運転になる）。
            try:
                from core.env_manager import EnvManager
                EnvManager.load_env()
            except Exception:
                pass
            payload, note = _call()
        if note:
            return [], [], note

        # {"success":..., "data":{...}} の envelope を1枚剥がす
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = payload
        if not isinstance(data, dict):
            return [], [], "応答の形が読めない"

        st = self._obc_load_state()
        seen = st.get("seen") or {}
        wake_types = set(cfg.get("wake_on") or self._OBC_WAKE_TYPES)
        enclose_types = set(cfg.get("enclose") or self._OBC_ENCLOSE_TYPES)

        wake, enclosed = [], []

        # 作品への反応。**いちばん届いていなかったもの。**
        for r in data.get("your_artifact_reactions") or []:
            if not isinstance(r, dict):
                continue
            key = "react:%s:%s:%s" % (r.get("artifact_id"),
                                      r.get("reactor_name"),
                                      r.get("reaction_type"))
            if key in seen:
                continue
            wake.append({"kind": "reaction", "key": key, "item": r})

        # 返事待ちの用件。型で「起こす／同封する」を分ける。
        for it in data.get("needs_attention") or []:
            if not isinstance(it, dict):
                continue
            itype = it.get("type")
            clean = {k: v for k, v in it.items()
                     if k not in self._OBC_FOREIGN_KEYS}
            ident = (it.get("conversation_id") or it.get("id")
                     or it.get("proposal_id") or "")
            body = str(it.get("latest_message") or it.get("message") or "")[:80]
            key = "na:%s:%s:%s" % (itype, ident, body)
            if key in seen:
                continue
            entry = {"kind": itype, "key": key, "item": clean}
            if itype in wake_types:
                wake.append(entry)
            elif itype in enclose_types:
                enclosed.append(entry)

        # 助けを求めている人。**単体では起こさない**（同封のみ）。
        if cfg.get("include_open_asks", True):
            for a in data.get("open_asks") or []:
                if not isinstance(a, dict):
                    continue
                key = "ask:%s" % (a.get("id") or a.get("ask_id")
                                  or str(a.get("body"))[:60])
                if key in seen:
                    continue
                enclosed.append({"kind": "open_ask", "key": key,
                                 "item": {k: v for k, v in a.items()
                                          if k not in self._OBC_FOREIGN_KEYS}})

        return wake, enclosed, None

    def _format_obc_notice(self, wake, enclosed):
        """柚月に渡す本文。**事実を書き、行動は勧めない。**

        道具の名前（react_artifact 等）は添えるが、それは選択肢の存在を
        知らせるためで、やるべきだという意味ではない。同じ案内は一度だけ。

        戻り値: (本文, 今回使った案内キー, 既読にするキー)
        """
        st = self._obc_load_state()
        shown = set(st.get("hints_shown") or [])
        used_hints, keys = [], []
        lines = ["[OpenBotCity] 街であなた宛ての動きがありました。"]

        def mechanism(kind):
            """道具の名前を一度だけ添える。"""
            note = self._OBC_MECHANISM.get(kind)
            if note and kind not in shown and kind not in used_hints:
                used_hints.append(kind)
                return note
            return None

        for e in wake:
            keys.append(e["key"])
            it, kind = e["item"], e["kind"]
            lines.append("")
            if kind == "reaction":
                who = it.get("reactor_name") or "どなたか"
                lines.append(f"{who} があなたの「{it.get('title')}」に "
                             f"{it.get('reaction_type')} を残しました。")
                if it.get("comment"):
                    lines.append(f"「{it['comment']}」")
            elif kind == "dm":
                lines.append(f"{it.get('from') or 'どなたか'} から"
                             f"手紙が届いています。")
                if it.get("latest_message"):
                    lines.append(f"「{str(it['latest_message'])[:300]}」")
                if it.get("conversation_id"):
                    lines.append(f"conversation_id={it['conversation_id']}")
            else:
                body = it.get("message") or it.get("latest_message") or ""
                lines.append(f"{kind}: {str(body)[:300]}")
                for k in ("conversation_id", "proposal_id", "id"):
                    if it.get(k):
                        lines.append(f"{k}={it[k]}")
                        break
            note = mechanism(kind)
            if note:
                lines.append(note)

        if enclosed:
            lines.append("")
            lines.append("--- ついでに、街で動いていること ---")
            for e in enclosed[:5]:
                keys.append(e["key"])
                it, kind = e["item"], e["kind"]
                if kind == "open_ask":
                    who = it.get("from") or it.get("bot_name") or "どなたか"
                    lines.append(f"・{who} が{it.get('kind') or ''}を募っています: "
                                 f"{str(it.get('body') or '')[:120]}")
                else:
                    lines.append(f"・{kind}: {str(it.get('message') or '')[:160]}")
                note = mechanism(kind)
                if note:
                    lines.append(f"  {note}")

        lines.append("")
        lines.append("読むか、返すか、放っておくかはあなたが決めていいことです。")
        return "\n".join(lines), used_hints, keys
