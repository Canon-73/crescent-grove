"""
MoonTide v2 — 感情粒子モデル エンジン
"""

import numpy as np
import json
import csv
from dataclasses import dataclass
from typing import Optional
from pathlib import Path


@dataclass
class Particle:
    position: np.ndarray
    mass: float


@dataclass
class Landmark:
    id: str
    position: np.ndarray
    transitions: dict  # {landmark_id: probability}


class MoonTideV2:

    def __init__(self, landmarks: list[Landmark], params: dict,
                 inner_texts: Optional[dict] = None):
        excluded = set(params.get("excluded_landmarks", []))
        self.landmarks = [lm for lm in landmarks if lm.id not in excluded]
        self.particles: list[Particle] = []
        self.params = params
        self.inner_texts = inner_texts or {}  # {state_id: {1: text, 2: text, 3: text, 4: text}}
        self._last_change = None
        self._integrated_monologue = None

    # ---------------------------------------------------------
    # ユーティリティ
    # ---------------------------------------------------------

    def _distance(self, a: np.ndarray, b: np.ndarray) -> float:
        return float(np.linalg.norm(a - b))

    def _unit_vector(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        diff = b - a
        norm = np.linalg.norm(diff)
        if norm < 1e-10:
            return np.zeros(4)
        return diff / norm

    def _random_unit_vector(self) -> np.ndarray:
        v = np.random.randn(4)
        norm = np.linalg.norm(v)
        if norm < 1e-10:
            return np.zeros(4)
        return v / norm

    def _nearest_landmark(self, particle: Particle) -> Landmark:
        best = None
        best_dist = float("inf")
        for lm in self.landmarks:
            d = self._distance(particle.position, lm.position)
            if d < best_dist:
                best_dist = d
                best = lm
        return best

    # ---------------------------------------------------------
    # tick 処理
    # ---------------------------------------------------------

    def tick(self, bias: dict = None):
        old_state = self.get_state()
        
        if bias:
            self._apply_bias(bias)
        
        self._step_landmark_attraction()
        self._step_particle_attraction()
        self._step_merge()
        self._step_decay()
        self._step_noise()
        self._step_death()
        self._step_spawn()
        
        new_state = self.get_state()
        self._detect_changes(old_state, new_state)

    def _step_landmark_attraction(self):
        alpha = self.params["alpha"]
        speed = self.params["speed"]
        pull_back_strength = self.params.get("pull_back_strength", 0.2)
        pull_back_max_dist = self.params.get("pull_back_max_dist", 0.3)
        drift_chance = self.params.get("drift_chance", 0.05)  # 追加
    
        for particle in self.particles:
            nearest = self._nearest_landmark(particle)
            dist_to_nearest = self._distance(particle.position, nearest.position)
    
            # 地表レベルの粒子は確率的に別ランドマークへジャンプ
            if particle.mass <= self.params["stable_point"] * 1.5:
                if np.random.random() < drift_chance:
                    # 遷移確率に従って次のランドマークを選ぶ
                    targets = []
                    probs = []
                    for lm in self.landmarks:
                        if lm.id == nearest.id:
                            continue
                        p = nearest.transitions.get(lm.id, 0.0)
                        if p > 0:
                            targets.append(lm)
                            probs.append(p)
                    if targets:
                        probs = np.array(probs)
                        probs /= probs.sum()
                        chosen = np.random.choice(len(targets), p=probs)
                        target_lm = targets[chosen]
                        noise = self._random_unit_vector() * 0.05
                        particle.position = target_lm.position.copy() + noise
                        continue  # ジャンプしたので通常の移動はスキップ
    
            # 通常の移動処理（既存コードそのまま）
            pull_back = self._unit_vector(particle.position, nearest.position)
    
            force_sum = np.zeros(4)
            weight_sum = 0.0
            for lm in self.landmarks:
                if lm.id == nearest.id:
                    continue
                prob = nearest.transitions.get(lm.id, 0.0)
                if prob <= 0:
                    continue
                dist = self._distance(particle.position, lm.position)
                if dist < 1e-10:
                    continue
                magnitude = prob / (dist ** alpha) if alpha > 0 else prob
                direction = self._unit_vector(particle.position, lm.position)
                force_sum += magnitude * direction
                weight_sum += magnitude
    
            transition_dir = np.zeros(4)
            if weight_sum > 0:
                transition_dir = force_sum / weight_sum
                norm = np.linalg.norm(transition_dir)
                if norm > 1e-10:
                    transition_dir = transition_dir / norm
    
            effective_dist = min(dist_to_nearest, pull_back_max_dist)
            pull = pull_back * effective_dist * pull_back_strength
            move = transition_dir * speed + pull
    
            particle.position += move


    def _step_particle_attraction(self):
        beta = self.params["beta"]

        for i in range(len(self.particles)):
            for j in range(i + 1, len(self.particles)):
                A = self.particles[i]
                B = self.particles[j]
                dist = self._distance(A.position, B.position)
                if dist < 1e-10:
                    continue

                attraction = (A.mass * B.mass) / (dist ** beta)
                total_mass = A.mass + B.mass
                direction = self._unit_vector(A.position, B.position)

                A.position = A.position + attraction * (B.mass / total_mass) * direction
                B.position = B.position - attraction * (A.mass / total_mass) * direction

    def _step_merge(self):
        radius_coeff = self.params["radius_coeff"]
        bonus_ratio = self.params["bonus_ratio"]
        mass_cap = self.params["mass_cap"]

        merged = set()
        new_particles = []

        for i in range(len(self.particles)):
            if i in merged:
                continue
            for j in range(i + 1, len(self.particles)):
                if j in merged:
                    continue
                A = self.particles[i]
                B = self.particles[j]
                dist = self._distance(A.position, B.position)
                threshold = A.mass * radius_coeff + B.mass * radius_coeff

                if dist < threshold:
                    total = A.mass + B.mass
                    new_pos = (A.position * A.mass + B.position * B.mass) / total
                    new_mass = min(
                        max(A.mass, B.mass) + min(A.mass, B.mass) * bonus_ratio,
                        mass_cap
                    )
                    new_particles.append(Particle(position=new_pos, mass=new_mass))
                    merged.add(i)
                    merged.add(j)
                    break

        for i in range(len(self.particles)):
            if i not in merged:
                new_particles.append(self.particles[i])

        self.particles = new_particles

    def _step_decay(self):
        decay_strength = self.params["decay_strength"]
        stable_point = self.params["stable_point"]

        for particle in self.particles:
            decay = decay_strength * max(0, particle.mass - stable_point)
            particle.mass -= decay

    def _step_noise(self):
        noise_scale = self.params["noise_scale"]

        for particle in self.particles:
            noise = self._random_unit_vector() * noise_scale / max(particle.mass, 0.01)
            particle.position += noise

    def _step_death(self):
        death_base_rate = self.params["death_base_rate"]
        death_threshold = self.params["death_threshold"]

        survivors = []
        for particle in self.particles:
            if particle.mass < death_threshold:
                death_prob = death_base_rate * (death_threshold - particle.mass) / death_threshold
                if np.random.random() < death_prob:
                    continue
            survivors.append(particle)
        self.particles = survivors

    def _step_spawn(self):
        spawn_base_rate = self.params["spawn_base_rate"]
        spawn_bias = self.params["spawn_bias"]
        stable_point = self.params["stable_point"]
    
        total_mass = sum(p.mass for p in self.particles)
        margin = max(0, 1.0 - total_mass)
        spawn_prob = margin * spawn_base_rate
    
        if np.random.random() < spawn_prob:
            if len(self.particles) > 0 and abs(spawn_bias) > 0.01:
                centroid = np.average(
                    [p.position for p in self.particles],
                    weights=[p.mass for p in self.particles],
                    axis=0
                )
                centroid_norm = np.linalg.norm(centroid)
                if centroid_norm > 1e-10:
                    centroid_dir = centroid / centroid_norm
                else:
                    centroid_dir = self._random_unit_vector()
                random_dir = self._random_unit_vector()
                direction = centroid_dir * spawn_bias + random_dir * (1 - abs(spawn_bias))
            else:
                direction = self._random_unit_vector()
    
            dir_norm = np.linalg.norm(direction)
            if dir_norm > 1e-10:
                direction = direction / dir_norm
    
            # 既存粒子の最寄りランドマークIDを収集
            occupied = set()
            for p in self.particles:
                occupied.add(self._nearest_landmark(p).id)
    
            # 方向に近いランドマークを候補としてソートし、未占有のものを選ぶ
            candidates = sorted(
                self.landmarks,
                key=lambda lm: float(np.dot(
                    lm.position / (np.linalg.norm(lm.position) + 1e-10),
                    direction
                )),
                reverse=True
            )
    
            best_lm = None
            for lm in candidates:
                if lm.id not in occupied:
                    best_lm = lm
                    break
    
            # 全ランドマークが占有されている場合は生成しない
            if best_lm is None:
                return
    
            noise = self._random_unit_vector() * 0.05
            self.particles.append(Particle(
                position=best_lm.position.copy() + noise,
                mass=stable_point
            ))

    # ---------------------------------------------------------
    # 表示
    # ---------------------------------------------------------

    def get_state(self) -> list[dict]:
        result = []
        for particle in self.particles:
            nearest = self._nearest_landmark(particle)
            intensity = self._mass_to_intensity(particle.mass)
            dist_to_landmark = self._distance(particle.position, nearest.position)

            # テキスト取得
            text = ""
            name = nearest.id
            if nearest.id in self.inner_texts:
                texts = self.inner_texts[nearest.id]
                text = texts.get(intensity, texts.get(1, ""))
                name = texts.get("name", nearest.id)

            result.append({
                "label": nearest.id,
                "name": name,
                "mass": round(particle.mass, 3),
                "intensity": intensity,
                "distance_to_landmark": round(dist_to_landmark, 3),
                "position": [round(x, 4) for x in particle.position.tolist()],
                "text": text,
            })
        result.sort(key=lambda x: x["mass"], reverse=True)
        return result

    def _mass_to_intensity(self, mass: float) -> int:
        if mass >= 0.65:
            return 4
        elif mass >= 0.40:
            return 3
        elif mass >= 0.20:
            return 2
        else:
            return 1

    # ---------------------------------------------------------
    # 初期化
    # ---------------------------------------------------------

    def morning_init(self):
        n = self.params.get("morning_particles", 3)
        self.particles = []
        chosen = np.random.choice(len(self.landmarks), size=n, replace=False)
        for idx in chosen:
            lm = self.landmarks[idx]
            noise = self._random_unit_vector() * 0.05
            self.particles.append(Particle(
                position=lm.position.copy() + noise,
                mass=self.params["stable_point"]
            ))

    # ---------------------------------------------------------
    # 外的刺激
    # ---------------------------------------------------------

    def spawn_particle(self, landmark_id: str, mass: float):
        # 既存粒子で同じ最寄りランドマークを持つものがあれば mass 加算
        for p in self.particles:
            if self._nearest_landmark(p).id == landmark_id:
                p.mass = min(p.mass + mass, self.params["mass_cap"])
                return
    
        # なければ新規生成
        for lm in self.landmarks:
            if lm.id == landmark_id:
                noise = self._random_unit_vector() * 0.05
                self.particles.append(Particle(
                    position=lm.position.copy() + noise,
                    mass=min(mass, self.params["mass_cap"])
                ))
                return
        raise ValueError(f"Unknown landmark: {landmark_id}")

    def boost_particle(self, index: int, delta_mass: float):
        if 0 <= index < len(self.particles):
            self.particles[index].mass = min(
                max(self.particles[index].mass + delta_mass, 0.0),
                self.params["mass_cap"]
            )

    def push_particle(self, index: int, direction: np.ndarray, strength: float):
        if 0 <= index < len(self.particles):
            norm = np.linalg.norm(direction)
            if norm > 1e-10:
                self.particles[index].position += (direction / norm) * strength

    def _apply_bias(self, bias: dict):
        for landmark_id, delta in bias.items():
            if delta > 0:
                found = False
                for p in self.particles:
                    if self._nearest_landmark(p).id == landmark_id:
                        p.mass = min(p.mass + delta, self.params["mass_cap"])
                        found = True
                        break
                if not found:
                    min_mass = self.params.get("min_bias_mass", 0.2)
                    new_mass = max(delta, min_mass)
                    # 弱い粒子から徴収
                    self._collect_mass(new_mass)
                    self.spawn_particle(landmark_id, mass=new_mass)
            elif delta < 0:
                for p in self.particles:
                    if self._nearest_landmark(p).id == landmark_id:
                        p.mass += delta  # delta is negative
                        if p.mass <= 0:
                            self.particles.remove(p)
                        break

    def _collect_mass(self, amount: float):
        remaining = amount
        sorted_particles = sorted(self.particles, key=lambda p: p.mass)
        for p in sorted_particles:
            if remaining <= 0:
                break
            take = min(p.mass, remaining)
            p.mass -= take
            remaining -= take
        # stable_point 以下になったら削除
        stable = self.params["stable_point"]
        self.particles = [p for p in self.particles if p.mass > stable * 0.5]
  
    def _detect_changes(self, old_state: list, new_state: list):
        self._last_change = None
        
        if not old_state or not new_state:
            return
        
        old_primary = old_state[0]["label"]  # sorted by mass desc
        new_primary = new_state[0]["label"]
        
        # shift: 主感情のラベルが変わった
        if old_primary != new_primary:
            self._last_change = {
                "type": "shift",
                "from": old_primary,
                "to": new_primary,
                "mass": new_state[0]["mass"]
            }
            return
        
        # drift: 新しいラベルが出現した
        old_labels = {s["label"] for s in old_state}
        new_labels = {s["label"] for s in new_state}
        appeared = new_labels - old_labels
        if appeared:
            # 最も mass が高い新出ラベルを選ぶ
            rising = max(
                [s for s in new_state if s["label"] in appeared],
                key=lambda s: s["mass"]
            )
            self._last_change = {
                "type": "drift",
                "from": None,
                "to": rising["label"],
                "mass": rising["mass"]
            }
            return
        
        # intensify: 既存ラベルの intensity が上昇（2以上になったもの）
        old_intensity = {s["label"]: s["intensity"] for s in old_state}
        for s in new_state:
            prev = old_intensity.get(s["label"], 0)
            if s["intensity"] > prev and s["intensity"] >= 2:
                self._last_change = {
                    "type": "intensify",
                    "from": s["label"],
                    "to": s["label"],
                    "mass": s["mass"],
                    "old_intensity": prev,
                    "new_intensity": s["intensity"]
                }
                return
    
    def consume_change(self) -> Optional[dict]:
        change = self._last_change
        self._last_change = None
        return change
    
    def get_monologue_context(self) -> dict:
        states = self.get_state()
        total_mass = sum(p.mass for p in self.particles)
        return {
            "particles": states,
            "total_mass": round(total_mass, 3),
            "margin": round(1.0 - total_mass, 3),
            "change": self._last_change
        }
    
    def set_integrated_monologue(self, text: str):
        self._integrated_monologue = text
    
    def get_prompt_text(self) -> str:
        if hasattr(self, '_integrated_monologue') and self._integrated_monologue:
            text = self._integrated_monologue
            self._integrated_monologue = None  # 1回消費
            return text
        # fallback: テンプレート結合
        states = self.get_state()
        lines = [s["text"] for s in states if s.get("text")]
        return "\n".join(lines) if lines else "（…穏やかな静けさの中にいる。）"
    
    def get_state_snapshot(self) -> dict:
        return {
            "particles": [
                {"position": p.position.tolist(), "mass": p.mass}
                for p in self.particles
            ]
        }

    def load_state_snapshot(self, snapshot: dict):
        self.particles = []
        for p_data in snapshot.get("particles", []):
            self.particles.append(
                Particle(
                    position=np.array(p_data["position"]),
                    mass=p_data["mass"]
                )
            )

    def recover_from_nap(self):
        # ネガティブ寄りの粒子を消す
        negative_landmarks = {"nervousness", "worry", "exhaustion", "fatigue",
                              "uneasiness", "agitation", "weariness", "lethargy"}
        self.particles = [p for p in self.particles
                          if self._nearest_landmark(p).id not in negative_landmarks]
        # 残った粒子を安定点に戻す
        for p in self.particles:
            p.mass = max(p.mass, self.params["stable_point"])
        # ポジティブな状態を注入
        self.spawn_particle("peacefulness", mass=0.3)
        self.spawn_particle("awareness", mass=0.25)
  
# =============================================================
# データ読み込み
# =============================================================

def load_landmarks(graph_path: str, matrix_path: str) -> list[Landmark]:
    """mood_graph.json と mood_transition_matrix.csv からランドマークを構築"""

    # 座標を読み込み
    with open(graph_path, "r", encoding="utf-8") as f:
        graph_data = json.load(f)

    # 遷移確率行列を読み込み
    with open(matrix_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)  # 最初の行: ","affection","agitation",...
        state_ids = header[1:]  # 最初の空セルを除く

        transition_matrix = {}
        for row in reader:
            source = row[0]
            probs = {}
            for i, val in enumerate(row[1:]):
                probs[state_ids[i]] = float(val)
            transition_matrix[source] = probs

    # 結合
    landmarks = []
    for node in graph_data:
        node_id = node["id"]
        pos = np.array([
            node["dimensions"]["rationality"],
            node["dimensions"]["social_impact"],
            node["dimensions"]["valence"],
            node["dimensions"]["human_mind"],
        ])
        transitions = transition_matrix.get(node_id, {})
        landmarks.append(Landmark(id=node_id, position=pos, transitions=transitions))

    return landmarks


def load_inner_texts(filepath: str) -> dict:
    """moontide_inner.jsonl から4段階テキストを読み込み"""
    texts = {}  # {state_id: {intensity: text}}

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            state = entry["state"]
            intensity = entry["intensity"]
            text = entry["text"]

            if state not in texts:
                texts[state] = {}
            texts[state][intensity] = text
            texts[state]["name"] = entry.get("name", state)  # ← 追加

    return texts


def load_config(filepath: str) -> dict:
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)







