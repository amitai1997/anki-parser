import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser import _shuffle_booklet_options


def test_shuffle_preserves_correct_text():
    options = ["A", "B", "C", "D", "E"]
    shuffled, correct = _shuffle_booklet_options(options, "tag", 1)
    assert correct is not None
    assert shuffled[correct - 1] == options[0]
    assert sorted(shuffled) == sorted(options)


def test_shuffle_is_deterministic():
    options = ["alpha", "beta", "gamma", "delta", "epsilon"]
    a = _shuffle_booklet_options(options, "2024-moed-a", 7)
    b = _shuffle_booklet_options(options, "2024-moed-a", 7)
    assert a == b


def test_shuffle_varies_across_qnums():
    options = ["one", "two", "three", "four", "five"]
    positions = {
        _shuffle_booklet_options(options, "tag", q)[1] for q in range(1, 21)
    }
    assert positions != {1}


def test_shuffle_empty_options():
    shuffled, correct = _shuffle_booklet_options([], "tag", 1)
    assert shuffled == []
    assert correct is None


def test_shuffle_single_option():
    shuffled, correct = _shuffle_booklet_options(["only"], "tag", 1)
    assert shuffled == ["only"]
    assert correct == 1


def test_shuffle_duplicate_options():
    options = ["same", "same", "B", "C", "D"]
    shuffled, correct = _shuffle_booklet_options(options, "tag", 3)
    assert shuffled[correct - 1] == options[0]
    assert sorted(shuffled) == sorted(options)
