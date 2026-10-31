import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "scripts"))

FIXED_NOW = datetime(2026, 10, 31, 11, 0, 0)


@pytest.fixture()
def seeded_db(tmp_path):
    import seed
    path = tmp_path / "novatrust.sqlite3"
    seed.seed(path, 42)
    return path


@pytest.fixture()
def bank(seeded_db):
    from bank.db import connect
    from bank.mock_adapter import MockAdapter
    conn = connect(seeded_db)
    yield MockAdapter(conn, clock=lambda: FIXED_NOW)
    conn.close()
