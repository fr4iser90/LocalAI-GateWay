"""Browser timezone detection for traffic charts."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.data.db import hash_password
from app.data.models import AdminUser, Base, make_engine, make_session_factory
from app.stats import display_zone, is_valid_timezone, week_window_start, zone_from_request


class _Req:
    def __init__(self, cookies: dict):
        self.cookies = cookies


def test_display_zone_berlin():
    z = display_zone("Europe/Berlin")
    assert str(z) == "Europe/Berlin"
    assert str(display_zone("Not/AZone")) == "UTC"
    assert str(display_zone("")) == "UTC"
    assert is_valid_timezone("Europe/Berlin")
    assert not is_valid_timezone("Not/AZone")
    assert not is_valid_timezone("")


def test_week_window_uses_local_midnight():
    z = display_zone("Europe/Berlin")
    start = week_window_start(z)
    assert start.tzinfo is not None
    assert start.hour == 0 and start.minute == 0
    today = datetime.now(timezone.utc).astimezone(z).date()
    assert start.astimezone(z).date() == today - timedelta(days=6)


def test_zone_from_request_cookie_beats_user(tmp_path: Path):
    eng = make_engine(str(tmp_path / "tz.db"))
    Base.metadata.create_all(bind=eng)
    db = make_session_factory(eng)()
    user = AdminUser(
        username="a",
        password_hash=hash_password("x"),
        is_platform_admin=True,
        is_active=True,
        timezone="America/New_York",
    )
    db.add(user)
    db.commit()
    assert str(zone_from_request(_Req({"gw_tz": "Europe/Berlin"}), user)) == "Europe/Berlin"
    assert str(zone_from_request(_Req({}), user)) == "America/New_York"
    assert str(zone_from_request(_Req({"gw_tz": "Nope/Nope"}), user)) == "America/New_York"
    assert str(zone_from_request(None, None)) == "UTC"
