import json

from clipcrumble import subtitles
from clipcrumble.ingest import is_hallucination


def test_stock_phrases_dropped_real_lines_kept():
    for t in ["Thank[?] you.", "Transcription[?] by CastingWords", "We'll be right back.", "ご 視 聴 ありがとうございました"]:
        assert is_hallucination(t)
    for t in ["Thank you for saving her.", "何 の ところ を お 話 し し よう かな", ""]:
        assert not is_hallucination(t)


def q(text, start=1.0, frames=3, top=0.75):
    return dict(start=start, end=start + 1.5, text=text, box=[0.3, top, 0.4, 0.05], frames=frames)


def test_noise_filter():
    keep = ["Help me.", "Sis.", "I couldn't resist.", "Gunji."]
    drop = ["* 32-84", "• шя 88* * 32-84", "impictures", 'SPOOKY AND DEVILISHLY ENTERTAINING" Kristy Puchko']
    assert all(subtitles._looks_like_subtitle(q(t)) for t in keep)
    assert not any(subtitles._looks_like_subtitle(q(t)) for t in drop)


class FakeCampaign:
    def __init__(self, working):
        self.working = working


def test_clean_applies_overrides(tmp_path):
    raw = {"A": {"cues": [q("to guide lost spirits into the next worl", 27.0), q("Run away!", 7.5), q("* 32", 9.0)]}}
    (tmp_path / "subtitles.json").write_text(json.dumps(raw))
    (tmp_path / "subtitle_overrides.json").write_text(json.dumps([
        {"src": "A", "start": 27.0, "text": "to guide lost spirits into the next world."},
        {"src": "A", "start": 7.5, "drop": True}]))
    out = subtitles.clean(FakeCampaign(tmp_path))["A"]
    assert [c["text"] for c in out] == ["to guide lost spirits into the next world."]
    assert out[0]["verified"]


def test_in_range_needs_real_overlap():
    cues = [q("a", 1.0), q("b", 5.0)]
    assert [c["text"] for c in subtitles.in_range(cues, 2.0, 4.0)] == ["a"]
    assert subtitles.in_range(cues, 2.4, 4.9) == []
