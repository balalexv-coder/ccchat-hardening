"""Tests for per-user settings storage (#5)."""
import json

import pytest

from backend import settings_store as ss

BLOB = {"claudeAiOauth": {"accessToken": "sk-ant-oat-AAAsecret",
                          "refreshToken": "sk-ant-ort-BBBsecret",
                          "expiresAt": 1780924188634, "subscriptionType": "max"}}


def test_mask_never_reveals_value():
    assert ss.mask(None) is None
    assert ss.mask("") is None
    assert ss.mask("abcdef") == "…cdef"
    assert ss.mask("ab") == "…set"


def test_parse_credentials_accepts_dict_and_string():
    assert ss.parse_credentials(BLOB)["claudeAiOauth"]["refreshToken"].endswith("BBBsecret")
    assert ss.parse_credentials(json.dumps(BLOB))["claudeAiOauth"]["accessToken"].endswith("AAAsecret")


def test_parse_credentials_rejects_garbage():
    for bad in ["not json", "{}", json.dumps({"claudeAiOauth": {"accessToken": "x"}})]:
        with pytest.raises(ss.CredentialError):
            ss.parse_credentials(bad)


def test_set_get_and_public_view(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "u.json")
    ss.set_credentials("alice", json.dumps(BLOB))
    assert ss.get_refresh_token("alice").endswith("BBBsecret")
    assert ss.get_credentials("alice")["claudeAiOauth"]["accessToken"].endswith("AAAsecret")

    v = ss.public_view("alice", is_admin=False)
    assert v["credentials_set"] is True
    assert v["refresh_token_masked"] == "…cret"
    assert v["subscription"] == "max"
    # raw secrets must NEVER appear in what the client sees
    assert "AAAsecret" not in json.dumps(v) and "BBBsecret" not in json.dumps(v)


def test_public_view_empty_for_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "u.json")
    v = ss.public_view("nobody", is_admin=True)
    assert v["credentials_set"] is False
    assert v["refresh_token_masked"] is None
    assert v["is_admin"] is True
