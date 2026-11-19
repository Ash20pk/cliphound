import numpy as np
import pytest

from cliphound import analyze, render, sources


@pytest.mark.parametrize("s,want", [("1:02:03", 3723), ("1h2m3s", 3723), ("62m", 3720), ("90", 90), ("1:30", 90)])
def test_parse_time(s, want):
    assert sources.parse_time(s) == want


def test_parse_time_rejects_junk():
    with pytest.raises(ValueError):
        sources.parse_time("soon")


def test_candidates_do_not_overlap():
    score = np.zeros(2000)
    score[[100, 110, 600, 1500]] = [10, 9, 8, 7]
    cands = analyze.candidates(score, 10, 30, 1000)
    assert [c["peak"] for c in cands] == [50.0, 300.0, 750.0]


def test_caption_chunks_break_on_punctuation_and_gaps():
    w = lambda t, s, e: {"w": t, "s": s, "e": e}
    words = [w("no", 0, .2), w("way,", .3, .5), w("bro", .6, .8), w("what", 2.0, 2.2)]
    assert [[x["w"] for x in c] for c in render._chunks(words)] == [["no", "way,"], ["bro"], ["what"]]


def test_a_caption_font_is_always_found():
    assert render._font(40) is not None
