
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ...database import crud, external_batch_crud


def recover_interrupted_external_batches(db: Session) -> int:
    recovered = 0
    for batch in external_batch_crud.list_recoverable_batches(db):
        items = external_batch_crud.list_batch_items(db, batch.batch_uuid)
        for item in items:
            if item.status in {'pending', 'running'}:
                external_batch_crud.update_batch_item(
                    db,
                    item.id,
                    status='failed',
                    failure_reason='service_restarted',
                    upload_status='failed' if item.upload_status == 'pending' else item.upload_status,
                    completed_at=datetime.utcnow(),
                )
            task = crud.get_registration_task_by_uuid(db, item.registration_task_uuid) if item.registration_task_uuid else None
            if task and task.status in {'pending', 'running'}:
                task.status = 'failed'
                task.error_message = 'service_restarted'
                task.completed_at = datetime.utcnow()
        external_batch_crud.update_batch(
            db,
            batch.batch_uuid,
            status='failed',
            failure_reason='service_restarted',
            completed_at=datetime.utcnow(),
        )
        recovered += 1
    db.commit()
    return recovered
