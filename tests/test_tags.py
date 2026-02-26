"""Tests for tags.py — deploy tag history."""

from flow_deploy.tags import current_tag, previous_tag, read_tags, write_tag


def test_read_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert read_tags() == []


def test_write_and_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_tag("abc123")
    assert read_tags() == ["abc123"]
    assert current_tag() == "abc123"


def test_multiple_tags(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_tag("v1")
    write_tag("v2")
    write_tag("v3")
    assert read_tags() == ["v1", "v2", "v3"]
    assert current_tag() == "v3"
    assert previous_tag() == "v2"


def test_max_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for i in range(15):
        write_tag(f"tag-{i}")
    tags = read_tags()
    assert len(tags) == 10
    assert tags[0] == "tag-5"
    assert tags[-1] == "tag-14"


def test_current_tag_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert current_tag() is None


def test_previous_tag_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert previous_tag() is None


def test_previous_tag_single(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_tag("only-one")
    assert previous_tag() is None
