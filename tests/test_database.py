from vfe_bot.database import Database


def test_pair_and_atomic_approval(tmp_path) -> None:
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    assert db.get_owner() is None
    assert db.set_owner(10, 20, "owner")
    assert db.is_owner(10, 20)
    assert not db.is_owner(10, 21)

    approval_id = db.create_approval(10, "restart", "plex", {"container_id": "abc"})
    first = db.claim_approval(approval_id, 10)
    assert first is not None
    assert first["payload"]["container_id"] == "abc"
    assert db.claim_approval(approval_id, 10) is None
    db.finish_approval(approval_id, "success")
