"""Unit tests for the admin session-overview / idle-reaping helpers (pure parsing logic)."""
from backend.manager import Manager


def test_parse_mem_mb_units():
    p = Manager._parse_mem_mb
    assert p("512MiB") == 512.0
    assert p("1.5GiB") == 1536.0
    assert p("800KiB") == round(800 / 1024, 1)
    assert p("123.4MB") == 123.4
    assert p("2GiB") == 2048.0


def test_parse_mem_mb_garbage_is_zero():
    p = Manager._parse_mem_mb
    assert p("") == 0.0
    assert p("--") == 0.0
    assert p("N/A") == 0.0
