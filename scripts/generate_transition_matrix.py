# generate_transition_matrix.py（改良版）
import numpy as np
import pandas as pd
from scipy.spatial.distance import cdist
from scipy.optimize import minimize_scalar

# 1. データ読み込み
pc60 = pd.read_csv('osfstorage-archive/study4/pc60.csv', index_col=0)
states = list(pc60.index)
coords = pc60.values

# 2. 距離行列
dist_matrix = cdist(coords, coords, metric='euclidean')

# 3. キャリブレーション
ground_truth = pd.read_csv('osfstorage-archive/study3/ground_transition_odds.csv', index_col=0)
overlap = {'Awe': 'awe', 'Disgust': 'disgust', 
           'Embarrassment': 'embarrassment', 'Satisfaction': 'satisfaction'}
overlap_s3 = list(overlap.keys())

actual_odds = []
predicted_dists = []
for s1 in overlap_s3:
    for s2 in overlap_s3:
        if s1 != s2:
            actual_odds.append(ground_truth.loc[s1, s2])
            i = states.index(overlap[s1])
            j = states.index(overlap[s2])
            predicted_dists.append(dist_matrix[i, j])

actual_odds = np.array(actual_odds)
predicted_dists = np.array(predicted_dists)

result = minimize_scalar(lambda b: -np.corrcoef(np.exp(-b * predicted_dists), actual_odds)[0, 1],
                         bounds=(0.1, 5.0), method='bounded')
beta_opt = result.x

# 4. 自己持続バイアスの算出
# study3の実測データから：自己遷移オッズの平均
self_odds = np.array([ground_truth.iloc[i, i] for i in range(len(ground_truth))])
other_odds = []
for i in range(len(ground_truth)):
    row = ground_truth.iloc[i].values
    other_odds.append(np.mean([row[j] for j in range(len(row)) if j != i]))
other_odds = np.array(other_odds)

# 自己持続は他状態への遷移の何倍か
self_ratio = np.mean(self_odds / other_odds)
print(f"自己持続倍率（実測平均）: {self_ratio:.2f}x")

# 5. 遷移確率行列の生成（自己持続バイアス付き）
def softmax_transition_with_self_bias(dist_matrix, beta, self_bias):
    n = dist_matrix.shape[0]
    similarity = np.exp(-beta * dist_matrix)
    # 対角（自己遷移）に倍率をかける
    for i in range(n):
        similarity[i, i] *= self_bias
    # 正規化
    row_sums = similarity.sum(axis=1, keepdims=True)
    prob_matrix = similarity / row_sums
    return prob_matrix

transition_prob = softmax_transition_with_self_bias(dist_matrix, beta_opt, self_ratio)

# 6. 保存
result_df = pd.DataFrame(transition_prob, index=states, columns=states)
result_df.to_csv('mood_transition_matrix.csv')

print(f"β: {beta_opt:.4f}")
print(f"相関: {-result.fun:.4f}")
print(f"\n✓ mood_transition_matrix.csv を保存しました")

# 7. サンプル表示
print(f"\n--- peacefulness からの遷移確率 TOP5 ---")
peace_idx = states.index('peacefulness')
top5 = np.argsort(transition_prob[peace_idx])[::-1][:6]
for idx in top5:
    label = "（自己）" if idx == peace_idx else ""
    print(f"  → {states[idx]}: {transition_prob[peace_idx, idx]:.4f} {label}")

print(f"\n--- nervousness からの遷移確率 TOP5 ---")
nerv_idx = states.index('nervousness')
top5 = np.argsort(transition_prob[nerv_idx])[::-1][:6]
for idx in top5:
    label = "（自己）" if idx == nerv_idx else ""
    print(f"  → {states[idx]}: {transition_prob[nerv_idx, idx]:.4f} {label}")

print(f"\n--- curiosity からの遷移確率 TOP5 ---")
cur_idx = states.index('curiosity')
top5 = np.argsort(transition_prob[cur_idx])[::-1][:6]
for idx in top5:
    label = "（自己）" if idx == cur_idx else ""
    print(f"  → {states[idx]}: {transition_prob[cur_idx, idx]:.4f} {label}")

print(f"\n自己遷移確率の範囲: {np.diag(transition_prob).min():.4f} ~ {np.diag(transition_prob).max():.4f}")
