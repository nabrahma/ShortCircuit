from unittest.mock import MagicMock, patch

from database import DatabaseManager


def _mock_connection(description, rows):
    cursor = MagicMock()
    cursor.description = description
    cursor.fetchall.return_value = rows
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = None

    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = None
    return conn


def test_query_returns_empty_list_when_no_rows():
    db = DatabaseManager()
    conn = _mock_connection(None, [])
    fake_psycopg2 = MagicMock()
    fake_psycopg2.connect.return_value = conn
    with patch("database.psycopg2", fake_psycopg2):
        result = db.query("SELECT 1")

    assert result == []


def test_query_returns_list_of_dicts():
    db = DatabaseManager()
    conn = _mock_connection(("col",), [{"symbol": "NSE:TEST-EQ", "pnl": 10.0}])
    fake_psycopg2 = MagicMock()
    fake_psycopg2.connect.return_value = conn
    with patch("database.psycopg2", fake_psycopg2):
        result = db.query("SELECT symbol, pnl FROM positions")

    assert isinstance(result, list)
    assert result[0]["symbol"] == "NSE:TEST-EQ"
