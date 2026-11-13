"""Tests for the shared environment-variable helper."""
import pytest

from shared.config import env


def test_missing_required_variable_raises_naming_it(monkeypatch):
    monkeypatch.delenv("BB_TEST_REQUIRED", raising=False)

    with pytest.raises(RuntimeError, match="BB_TEST_REQUIRED"):
        env("BB_TEST_REQUIRED")


def test_blank_required_variable_is_treated_as_missing(monkeypatch):
    monkeypatch.setenv("BB_TEST_REQUIRED", "   ")

    with pytest.raises(RuntimeError, match="BB_TEST_REQUIRED"):
        env("BB_TEST_REQUIRED")


def test_default_is_used_when_variable_is_missing(monkeypatch):
    monkeypatch.delenv("BB_TEST_OPTIONAL", raising=False)

    assert env("BB_TEST_OPTIONAL", default="fallback") == "fallback"


def test_default_is_used_when_variable_is_blank(monkeypatch):
    monkeypatch.setenv("BB_TEST_OPTIONAL", "")

    assert env("BB_TEST_OPTIONAL", default=None) is None


def test_value_is_cast(monkeypatch):
    monkeypatch.setenv("BB_TEST_NUMBER", "42")

    assert env("BB_TEST_NUMBER", cast=int) == 42


def test_failed_cast_raises_value_error_naming_it(monkeypatch):
    monkeypatch.setenv("BB_TEST_NUMBER", "not-a-number")

    with pytest.raises(ValueError, match="BB_TEST_NUMBER"):
        env("BB_TEST_NUMBER", cast=int)
