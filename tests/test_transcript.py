from cliphound import analyze, download
from cliphound.config import DEFAULTS


def test_json3_words_get_their_own_times():
    raw = {"events": [
        {"tStartMs": 1000, "dDurationMs": 3000, "segs": [{"utf8": ">> no"}, {"utf8": " way", "tOffsetMs": 400}]},
        {"tStartMs": 2000, "dDurationMs": 10, "aAppend": 1, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 5000, "dDurationMs": 1000, "segs": [{"utf8": "two words"}]},
    ]}
    words = download.parse_json3(raw)
    assert [w["w"] for w in words] == ["no", "way", "two", "words"]
    assert words[0]["s"] == 1.0 and words[1]["s"] == 1.4
    assert words[2]["s"] == 5.0 and words[3]["s"] == 5.5    # line-timed words are spread out


def test_excited_stretch_beats_calm_talk():
    words, t = [], 0.0
    while t < 600:                       # ten minutes of calm talk, ~2 words a second
        words.append({"w": "okay", "s": t, "e": t + 0.4})
        t += 0.5
    t = 300.0
    for i in range(40):                  # a burst of "no no no" at 5:00
        words.append({"w": "no", "s": t, "e": t + 0.15})
        t += 0.2
    words.sort(key=lambda w: w["s"])
    clips, wave, total = analyze.find_clips_transcript(words, dict(DEFAULTS), 1, 30, 600, lambda m: None)
    assert clips[0]["start"] < 305 < clips[0]["end"]
    assert clips[0]["from_captions"] and len(wave) == 720


def test_segments_split_on_pauses():
    w = lambda t, s: {"w": t, "s": s, "e": s + 0.2}
    segs = analyze.segments_from_words([w("a", 0), w("b", 0.3), w("c", 2.0)])
    assert [s["text"] for s in segs] == ["a b", "c"]
