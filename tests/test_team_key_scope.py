from __future__ import annotations

from types import SimpleNamespace

from app.admin.access import can_access_key, owned_team_ids


def _user(*, uid: int, admin: bool = False, memberships: list | None = None):
    return SimpleNamespace(
        id=uid,
        is_platform_admin=admin,
        memberships=memberships or [],
    )


def _member(team_id: int, role: str = "member"):
    return SimpleNamespace(team_id=team_id, role=role)


def _key(*, owner: int | None, team: int | None):
    return SimpleNamespace(owner_user_id=owner, team_id=team)


def test_owned_team_ids_only_owner_role():
    user = _user(uid=1, memberships=[_member(10, "member"), _member(20, "owner")])
    assert owned_team_ids(user) == {20}


def test_member_sees_only_own_keys_when_teams_on():
    member = _user(uid=2, memberships=[_member(10, "member")])
    own = _key(owner=2, team=10)
    teammate = _key(owner=3, team=10)
    assert can_access_key(member, own, teams_enabled=True)
    assert not can_access_key(member, teammate, teams_enabled=True)


def test_team_owner_sees_all_keys_on_that_team():
    owner = _user(uid=2, memberships=[_member(10, "owner")])
    teammate = _key(owner=3, team=10)
    other_team = _key(owner=4, team=99)
    assert can_access_key(owner, teammate, teams_enabled=True)
    assert not can_access_key(owner, other_team, teams_enabled=True)


def test_platform_admin_sees_all_keys():
    admin = _user(uid=1, admin=True)
    foreign = _key(owner=9, team=10)
    assert can_access_key(admin, foreign, teams_enabled=True)
    assert can_access_key(admin, foreign, teams_enabled=False)


def test_without_teams_only_own_keys():
    user = _user(uid=2, memberships=[_member(10, "owner")])
    own = _key(owner=2, team=10)
    teammate = _key(owner=3, team=10)
    assert can_access_key(user, own, teams_enabled=False)
    assert not can_access_key(user, teammate, teams_enabled=False)
