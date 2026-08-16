from sqlalchemy import create_engine, text

from app.data.db import _migrate_service_name_to_chat


def test_migrate_llm_and_ollama_team_grants_to_single_chat():
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE service_grants (
                  id INTEGER PRIMARY KEY,
                  service VARCHAR(32) NOT NULL,
                  api_key_id INTEGER,
                  team_id INTEGER,
                  UNIQUE(api_key_id, service),
                  UNIQUE(team_id, service)
                )
                """
            )
        )
        conn.execute(
            text("INSERT INTO service_grants(service, api_key_id, team_id) VALUES ('llm', NULL, 1)")
        )
        conn.execute(
            text(
                "INSERT INTO service_grants(service, api_key_id, team_id) VALUES ('ollama', NULL, 1)"
            )
        )
        conn.execute(
            text("INSERT INTO service_grants(service, api_key_id, team_id) VALUES ('stt', NULL, 1)")
        )
        _migrate_service_name_to_chat(conn, {"service_grants"})
        rows = list(
            conn.execute(
                text("SELECT service, team_id FROM service_grants ORDER BY service, team_id")
            )
        )
    assert ("chat", 1) in rows
    assert ("stt", 1) in rows
    assert not any(r[0] in ("llm", "ollama") for r in rows)
    assert sum(1 for r in rows if r[0] == "chat" and r[1] == 1) == 1
