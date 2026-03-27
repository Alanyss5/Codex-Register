
from pathlib import Path

import pytest

from src.database.models import Base, EmailService, CpaService, Sub2ApiService, TeamManagerService
from src.database.session import DatabaseSessionManager
from src.core.service_selection import (
    build_email_item_assignments,
    resolve_upload_target,
)


def _make_db(name: str):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        db_path.unlink()
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def test_temp_mail_assignments_only_use_highest_priority_pool():
    manager = _make_db("service_selection_temp_mail.db")
    with manager.session_scope() as session:
        session.add_all([
            EmailService(service_type="temp_mail", name="tm-a", config={"domain": "a.test"}, enabled=True, priority=0),
            EmailService(service_type="temp_mail", name="tm-b", config={"domain": "b.test"}, enabled=True, priority=0),
            EmailService(service_type="temp_mail", name="tm-c", config={"domain": "c.test"}, enabled=True, priority=10),
        ])

    with manager.session_scope() as session:
        assignments = build_email_item_assignments(session, email_type="temp_mail", count=12)

    assert len(assignments) == 12
    assert {item.service_id for item in assignments if item.service_id is not None} <= {1, 2}
    assert all(item.failure_reason is None for item in assignments)


def test_outlook_specific_service_cannot_be_reused_for_multiple_items():
    manager = _make_db("service_selection_outlook_specific.db")
    with manager.session_scope() as session:
        session.add(EmailService(service_type="outlook", name="outlook-1", config={"email": "a@example.com"}, enabled=True, priority=0))

    with manager.session_scope() as session:
        with pytest.raises(ValueError, match="count > 1"):
            build_email_item_assignments(session, email_type="outlook", count=2, requested_service_id=1)


def test_outlook_auto_selection_marks_overflow_items_unavailable():
    manager = _make_db("service_selection_outlook_auto.db")
    with manager.session_scope() as session:
        session.add_all([
            EmailService(service_type="outlook", name="outlook-1", config={"email": "a@example.com"}, enabled=True, priority=0),
            EmailService(service_type="outlook", name="outlook-2", config={"email": "b@example.com"}, enabled=True, priority=0),
        ])

    with manager.session_scope() as session:
        assignments = build_email_item_assignments(session, email_type="outlook", count=3)

    assert [item.service_id for item in assignments[:2]] == [1, 2]
    assert assignments[2].service_id is None
    assert assignments[2].failure_reason == "no_available_email_service"


def test_resolve_upload_target_uses_first_enabled_service_by_priority():
    manager = _make_db("service_selection_upload.db")
    with manager.session_scope() as session:
        session.add_all([
            Sub2ApiService(name="sub2-low", api_url="https://low.test", api_key="k1", enabled=True, priority=10),
            Sub2ApiService(name="sub2-high", api_url="https://high.test", api_key="k2", enabled=True, priority=0),
            CpaService(name="cpa-1", api_url="https://cpa.test", api_token="c1", enabled=True, priority=0),
            TeamManagerService(name="tm-1", api_url="https://tm.test", api_key="t1", enabled=True, priority=0),
        ])

    with manager.session_scope() as session:
        target = resolve_upload_target(session, provider="sub2api")

    assert target.provider == "sub2api"
    assert target.service_id == 2
    assert target.service_name == "sub2-high"


def test_resolve_upload_target_rejects_missing_or_mismatched_service():
    manager = _make_db("service_selection_upload_mismatch.db")
    with manager.session_scope() as session:
        session.add(CpaService(name="cpa-1", api_url="https://cpa.test", api_token="c1", enabled=True, priority=0))

    with manager.session_scope() as session:
        with pytest.raises(ValueError, match="does not belong"):
            resolve_upload_target(session, provider="sub2api", requested_service_id=1)


def test_build_email_item_assignments_rejects_invalid_count():
    manager = _make_db("service_selection_invalid_count.db")

    with manager.session_scope() as session:
        with pytest.raises(ValueError, match="count must be >= 1"):
            build_email_item_assignments(session, email_type="temp_mail", count=0)


def test_requested_email_service_must_exist_and_be_enabled():
    manager = _make_db("service_selection_requested_email.db")
    with manager.session_scope() as session:
        session.add(EmailService(service_type="temp_mail", name="tm-1", config={"domain": "a.test"}, enabled=False, priority=0))

    with manager.session_scope() as session:
        with pytest.raises(ValueError, match="not found"):
            build_email_item_assignments(session, email_type="temp_mail", count=1, requested_service_id=99)
        with pytest.raises(ValueError, match="is disabled"):
            build_email_item_assignments(session, email_type="temp_mail", count=1, requested_service_id=1)


def test_resolve_upload_target_rejects_unsupported_provider_and_disabled_service():
    manager = _make_db("service_selection_upload_rejects.db")
    with manager.session_scope() as session:
        session.add(Sub2ApiService(name="sub2-disabled", api_url="https://sub2.test", api_key="k1", enabled=False, priority=0))

    with manager.session_scope() as session:
        with pytest.raises(ValueError, match="unsupported upload provider"):
            resolve_upload_target(session, provider="unknown")
        with pytest.raises(ValueError, match="is disabled"):
            resolve_upload_target(session, provider="sub2api", requested_service_id=1)
        with pytest.raises(ValueError, match="no enabled upload services"):
            resolve_upload_target(session, provider="sub2api")
