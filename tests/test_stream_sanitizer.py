"""
StreamSanitizer テスト
隠しタグ（<internal> / [VITAL_REPORT:] / ツールXML）のストリーム除去
（pytest不要・python tests/test_stream_sanitizer.py で直接実行）

期待値は core/agent.py の最終クリーン処理と同じ正規表現で機械生成し、
「どんなチャンク分割でも feed結合+flush が期待値と一致する」ことを検証する。
"""
import random
import re
import sys
import os

# tests/ から見て1段上がリポジトリルート
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.stream_sanitizer import StreamSanitizer

# --- 期待値生成（core/agent.py の strip_tool_xml / _VITAL_REPORT_RE / _INTERNAL_RE と同一パターン） ---
_DSML_RE = re.compile(
    r'<\s*\|?\s*DSML\s*\|?\s*function_calls?\s*>.*?<\s*/\s*\|?\s*DSML\s*\|?\s*function_calls?\s*>',
    re.DOTALL,
)
_FUNCTION_CALL_RE = re.compile(r'<function_call>.*?</function_call>', re.DOTALL)
_VITAL_REPORT_RE = re.compile(r'\[VITAL_REPORT:.*?\]')
_INTERNAL_RE = re.compile(r'<internal>.*?</internal>', re.DOTALL)


def expected_clean(text: str) -> str:
    """サニタイザの理想出力（rstripはしない＝サニタイザはタグ除去のみ行う）"""
    text = _DSML_RE.sub('', text)
    text = _FUNCTION_CALL_RE.sub('', text)
    text = _VITAL_REPORT_RE.sub('', text)
    text = _INTERNAL_RE.sub('', text)
    return text


def run_sanitizer(chunks) -> str:
    s = StreamSanitizer()
    out = ""
    for c in chunks:
        out += s.feed(c)
    out += s.flush()
    return out


def all_splits_2(text):
    """全位置での2分割"""
    for i in range(len(text) + 1):
        yield [text[:i], text[i:]]


def random_chunks(text, rng):
    """ランダムな長さのチャンク列に分割"""
    chunks = []
    i = 0
    while i < len(text):
        n = rng.randint(1, 8)
        chunks.append(text[i:i + n])
        i += n
    return chunks


passed = 0
failed = 0


def run_test(name, fn):
    global passed, failed
    try:
        fn()
        print(f"[OK] {name}")
        passed += 1
    except AssertionError as e:
        print(f"[NG] {name}: {e}")
        failed += 1
    except Exception as e:
        print(f"[NG] {name}: 予期しない例外 {type(e).__name__}: {e}")
        failed += 1


# --- 閉じタグまで揃った文字列（分割不変性テスト対象） ---
WELL_FORMED_CASES = [
    "こんにちは、今日もいい天気ですね。",
    "前置き<internal>これは内面思考。見せてはいけない。</internal>後ろのテキスト",
    "数値は[VITAL_REPORT:hp=3,mp=5]こうなります",
    "A<function_call>{\"name\": \"read_file\"}</function_call>B",
    "x<|DSML|function_calls>呼び出し中身</|DSML|function_calls>y",
    "ws< | DSML | function_call >中身< / | DSML | function_call >末尾",
    "全部入り<internal>秘密1</internal>中間[VITAL_REPORT:a=1]さらに"
    "<function_call>f()</function_call>続き<internal>秘密2</internal>おわり",
    "数式 1 < 2 と a > b は本文として残る",
    "裸の [ かっこ や [NOTE: みたいなタグもどき] は残る",
    "<internal>冒頭からいきなり内面</internal>本文だけ残る",
    "改行込み<internal>複数行の\n内面思考\nです</internal>OK",
    "連接<a><internal>隠す</internal><b>タグもどきは残る",
    "絵文字🌙混じり<internal>秘密🌟</internal>も大丈夫✨",
    "[VITAL_REPORT:長い中身がチャンクをまたいでも問題ないことを確認する=1]end",
    "未知タグ<think>は仕様上そのまま残す</think>方針",
    "",
]


def test_well_formed_one_shot():
    """一括feedで期待値と一致"""
    for text in WELL_FORMED_CASES:
        got = run_sanitizer([text])
        want = expected_clean(text)
        assert got == want, f"text={text!r}\n got={got!r}\nwant={want!r}"


def test_well_formed_all_2_splits():
    """すべての位置で2分割しても結果が変わらない"""
    for text in WELL_FORMED_CASES:
        want = expected_clean(text)
        for chunks in all_splits_2(text):
            got = run_sanitizer(chunks)
            assert got == want, f"split={chunks!r}\n got={got!r}\nwant={want!r}"


def test_well_formed_char_by_char():
    """1文字ずつfeedしても結果が変わらない"""
    for text in WELL_FORMED_CASES:
        want = expected_clean(text)
        got = run_sanitizer(list(text))
        assert got == want, f"text={text!r}\n got={got!r}\nwant={want!r}"


def test_well_formed_random_chunks():
    """ランダム分割×100回でも結果が変わらない"""
    rng = random.Random(42)
    for text in WELL_FORMED_CASES:
        want = expected_clean(text)
        for _ in range(100):
            got = run_sanitizer(random_chunks(text, rng))
            assert got == want, f"text={text!r}\n got={got!r}\nwant={want!r}"


def test_no_secret_leak_any_split():
    """不変条件: どの分割でも internal / VITAL_REPORT の中身が1文字も出力されない"""
    secret_text = "前<internal>ヒミツのなかみ#</internal>中[VITAL_REPORT:kakushi=9]後"
    rng = random.Random(7)
    splits = list(all_splits_2(secret_text)) + [list(secret_text)] + [
        random_chunks(secret_text, rng) for _ in range(200)
    ]
    for chunks in splits:
        got = run_sanitizer(chunks)
        assert "ヒミツ" not in got and "なかみ" not in got and "#" not in got, \
            f"内面思考がリーク: chunks={chunks!r} got={got!r}"
        assert "kakushi" not in got and "9" not in got, \
            f"VITAL_REPORTがリーク: chunks={chunks!r} got={got!r}"


def test_unclosed_internal_discarded():
    """閉じタグの無い<internal>はflushで破棄される（隠す側に倒す）"""
    got = run_sanitizer(["本文<internal>閉じない秘密"])
    assert got == "本文", f"got={got!r}"


def test_unclosed_vital_discarded():
    """閉じない[VITAL_REPORT:もflushで破棄される"""
    got = run_sanitizer(["A[VITAL_REPORT:hp=", "3"])
    assert got == "A", f"got={got!r}"


def test_partial_tag_at_end_released():
    """タグ未成立の保留分（'<inter'で終わる等）はflushで本文として解放される"""
    got = run_sanitizer(["途中で切れた<inter"])
    assert got == "途中で切れた<inter", f"got={got!r}"
    got = run_sanitizer(["かっこ[VITAL"])
    assert got == "かっこ[VITAL", f"got={got!r}"


def test_long_non_tag_released():
    """'>'が来ないまま長く続く'<'はタグではないと判定して解放される"""
    text = "比較: x <" + "y" * 100 + " 終わり"
    got = run_sanitizer([text])
    assert got == text, f"got={got!r}"


def test_empty_deltas():
    """空deltaが混ざっても壊れない"""
    got = run_sanitizer(["", "abc", "", "<internal>x</internal>", "", "def", ""])
    assert got == "abcdef", f"got={got!r}"


def test_streaming_emits_early():
    """サニタイザが過剰に保留しない（タグの無い断片は即時出力される）"""
    s = StreamSanitizer()
    assert s.feed("こんにちは") == "こんにちは"
    assert s.feed("、続きです") == "、続きです"


if __name__ == "__main__":
    run_test("一括feed", test_well_formed_one_shot)
    run_test("全位置2分割", test_well_formed_all_2_splits)
    run_test("1文字ずつ", test_well_formed_char_by_char)
    run_test("ランダム分割×100", test_well_formed_random_chunks)
    run_test("秘密リーク無し（全分割）", test_no_secret_leak_any_split)
    run_test("閉じないinternalは破棄", test_unclosed_internal_discarded)
    run_test("閉じないVITALは破棄", test_unclosed_vital_discarded)
    run_test("タグ未成立はflushで解放", test_partial_tag_at_end_released)
    run_test("長い非タグ'<'は解放", test_long_non_tag_released)
    run_test("空delta", test_empty_deltas)
    run_test("即時出力", test_streaming_emits_early)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
