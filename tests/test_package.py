"""Tests for the maestro package."""

from io import StringIO
from unittest.mock import patch

import maestro
from main import main


def test_version() -> None:
    """Test that version is defined."""
    assert maestro.__version__ == "0.1.0"


def test_subpackages_importable() -> None:
    """Test that all subpackages can be imported."""
    import maestro.spawners
    import maestro.coordination
    import maestro.notifications

    assert maestro.spawners is not None
    assert maestro.coordination is not None
    assert maestro.notifications is not None


def test_main_prints_greeting() -> None:
    """Test that main() prints the expected greeting."""
    with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
        main()
        output = mock_stdout.getvalue()
    assert "Hello from maestro!" in output
