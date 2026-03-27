
from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from .models import ExternalRegistrationBatch, ExternalRegistrationBatchItem


def create_batch(db: Session, **kwargs) -> ExternalRegistrationBatch:
    batch = ExternalRegistrationBatch(**kwargs)
    db.add(batch)
    db.flush()
    db.refresh(batch)
    return batch


def create_batch_item(db: Session, **kwargs) -> ExternalRegistrationBatchItem:
    item = ExternalRegistrationBatchItem(**kwargs)
    db.add(item)
    db.flush()
    db.refresh(item)
    return item


def get_batch_by_uuid(db: Session, batch_uuid: str) -> Optional[ExternalRegistrationBatch]:
    return db.query(ExternalRegistrationBatch).filter(ExternalRegistrationBatch.batch_uuid == batch_uuid).first()


def get_batch_by_idempotency_key(db: Session, idempotency_key: str) -> Optional[ExternalRegistrationBatch]:
    if not idempotency_key:
        return None
    return db.query(ExternalRegistrationBatch).filter(ExternalRegistrationBatch.idempotency_key == idempotency_key).first()


def list_batch_items(db: Session, batch_uuid: str) -> list[ExternalRegistrationBatchItem]:
    return (
        db.query(ExternalRegistrationBatchItem)
        .join(ExternalRegistrationBatch, ExternalRegistrationBatch.id == ExternalRegistrationBatchItem.batch_id)
        .filter(ExternalRegistrationBatch.batch_uuid == batch_uuid)
        .order_by(ExternalRegistrationBatchItem.item_index.asc(), ExternalRegistrationBatchItem.id.asc())
        .all()
    )


def list_recoverable_batches(db: Session) -> list[ExternalRegistrationBatch]:
    return (
        db.query(ExternalRegistrationBatch)
        .filter(ExternalRegistrationBatch.status.in_(("pending", "running")))
        .order_by(ExternalRegistrationBatch.created_at.asc(), ExternalRegistrationBatch.id.asc())
        .all()
    )


def update_batch(db: Session, batch_uuid: str, **kwargs) -> Optional[ExternalRegistrationBatch]:
    batch = get_batch_by_uuid(db, batch_uuid)
    if not batch:
        return None
    for key, value in kwargs.items():
        setattr(batch, key, value)
    batch.updated_at = datetime.utcnow()
    db.flush()
    db.refresh(batch)
    return batch


def update_batch_item(db: Session, item_id: int, **kwargs) -> Optional[ExternalRegistrationBatchItem]:
    item = db.query(ExternalRegistrationBatchItem).filter(ExternalRegistrationBatchItem.id == item_id).first()
    if not item:
        return None
    for key, value in kwargs.items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    db.flush()
    db.refresh(item)
    return item


def list_items_by_status(db: Session, batch_uuid: str, statuses: Sequence[str]) -> list[ExternalRegistrationBatchItem]:
    return (
        db.query(ExternalRegistrationBatchItem)
        .join(ExternalRegistrationBatch, ExternalRegistrationBatch.id == ExternalRegistrationBatchItem.batch_id)
        .filter(
            ExternalRegistrationBatch.batch_uuid == batch_uuid,
            ExternalRegistrationBatchItem.status.in_(tuple(statuses)),
        )
        .order_by(ExternalRegistrationBatchItem.item_index.asc())
        .all()
    )
