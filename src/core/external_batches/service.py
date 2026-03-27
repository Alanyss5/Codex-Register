
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...database import crud, external_batch_crud
from ...database.models import Account, ExternalRegistrationBatchItem, RegistrationTask
from ..registration_upload import upload_registered_account
from ..service_selection import UploadTarget, build_email_item_assignments, resolve_upload_target


TERMINAL_ITEM_STATUSES = {'completed', 'failed', 'cancelled'}
CONFIG_FAILURE_PREFIXES = (
    'unsupported upload provider:',
    'no enabled upload services for provider ',
    'no enabled email services for type ',
)
BUSINESS_FAILURE_REASONS = {
    'outlook requested_service_id cannot be reused when count > 1',
}
TRANSIENT_FAILURE_REASONS = {
    'service_restarted',
    'no_available_email_service',
    'registration_failed',
}


class ExternalBatchCreateRequest(BaseModel):
    count: int
    idempotency_key: Optional[str] = None
    email_type: str
    email_service_id: Optional[int] = None
    upload_enabled: bool = False
    upload_provider: Optional[str] = None
    upload_service_id: Optional[int] = None
    mode: str = 'pipeline'
    concurrency: int = 1
    interval_min: int = 5
    interval_max: int = 30


class ExternalBatchService:
    @staticmethod
    def _mark_idempotent_replay(batch):
        if batch is not None:
            setattr(batch, '_idempotent_replay', True)
        return batch

    @staticmethod
    def _detach_batch(db: Session, batch):
        if batch is None:
            return None
        db.refresh(batch)
        db.expunge(batch)
        return batch

    def create_batch(self, db: Session, request: ExternalBatchCreateRequest):
        if request.idempotency_key:
            existing = external_batch_crud.get_batch_by_idempotency_key(db, request.idempotency_key)
            if existing:
                return self._mark_idempotent_replay(self._detach_batch(db, existing))

        upload_target = None
        if request.upload_enabled:
            upload_target = resolve_upload_target(
                db,
                provider=request.upload_provider or '',
                requested_service_id=request.upload_service_id,
            )

        assignments = build_email_item_assignments(
            db,
            email_type=request.email_type,
            count=request.count,
            requested_service_id=request.email_service_id,
        )

        batch_uuid = str(uuid4())
        batch = external_batch_crud.create_batch(
            db,
            batch_uuid=batch_uuid,
            idempotency_key=request.idempotency_key,
            status='pending',
            email_service_type=request.email_type,
            upload_enabled=request.upload_enabled,
            upload_provider=upload_target.provider if upload_target else None,
            requested_count=request.count,
            request_payload=request.model_dump(),
            runtime_snapshot={
                'email_assignments': [asdict(assignment) for assignment in assignments],
                'upload_target': asdict(upload_target) if upload_target else None,
            },
            recent_errors=[],
        )

        for assignment in assignments:
            task_uuid = str(uuid4())
            db.add(
                RegistrationTask(
                    task_uuid=task_uuid,
                    status='pending',
                    email_service_id=assignment.service_id,
                )
            )
            db.flush()
            external_batch_crud.create_batch_item(
                db,
                batch_id=batch.id,
                item_index=assignment.item_index,
                status='pending',
                failure_reason=assignment.failure_reason,
                registration_task_uuid=task_uuid,
                selected_email_service_id=assignment.service_id,
                upload_status='pending' if request.upload_enabled and not assignment.failure_reason else ('not_enabled' if not request.upload_enabled else 'skipped'),
            )

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if request.idempotency_key:
                existing = external_batch_crud.get_batch_by_idempotency_key(db, request.idempotency_key)
                if existing:
                    return self._mark_idempotent_replay(self._detach_batch(db, existing))
            raise

        return self._detach_batch(db, external_batch_crud.get_batch_by_uuid(db, batch_uuid))

    @staticmethod
    def _classify_failure_category(failure_reason: Optional[str]) -> str:
        reason = (failure_reason or '').strip().lower()
        if not reason:
            return 'transient'
        if reason in BUSINESS_FAILURE_REASONS:
            return 'business'
        if reason in TRANSIENT_FAILURE_REASONS:
            return 'transient'
        if reason.startswith(CONFIG_FAILURE_PREFIXES):
            return 'config'
        if reason.startswith('upload service ') and (
            ' not found' in reason
            or ' is disabled' in reason
            or ' does not belong to provider ' in reason
        ):
            return 'config'
        if reason.startswith('email service ') and (
            ' not found' in reason
            or ' is disabled' in reason
            or ' does not belong to type ' in reason
        ):
            return 'config'
        return 'transient'

    @staticmethod
    def _select_failure_reason(batch, items) -> Optional[str]:
        if batch.failure_reason:
            return batch.failure_reason
        for item in items:
            if item.failure_reason:
                return item.failure_reason
        for item in items:
            if item.upload_status == 'failed' and item.upload_error:
                return item.upload_error
        return None

    def recompute_summary(self, db: Session, batch_uuid: str):
        batch = external_batch_crud.get_batch_by_uuid(db, batch_uuid)
        if not batch:
            raise ValueError(f'batch not found: {batch_uuid}')

        items = external_batch_crud.list_batch_items(db, batch_uuid)
        completed_count = sum(1 for item in items if item.status in TERMINAL_ITEM_STATUSES)
        success_count = sum(1 for item in items if item.status == 'completed')
        failed_count = sum(1 for item in items if item.status == 'failed')
        cancelled_count = sum(1 for item in items if item.status == 'cancelled')
        upload_success_count = sum(1 for item in items if item.upload_status == 'success')
        upload_failed_count = sum(1 for item in items if item.upload_status == 'failed')

        recent_errors = []
        for item in items:
            if item.failure_reason:
                recent_errors.append({'item_index': item.item_index, 'reason': item.failure_reason})
            elif item.upload_status == 'failed' and item.upload_error:
                recent_errors.append({'item_index': item.item_index, 'reason': item.upload_error})

        if completed_count < batch.requested_count:
            status = 'running' if batch.started_at else 'pending'
            completed_at = None
        elif batch.cancel_requested or cancelled_count:
            status = 'cancelled'
            completed_at = datetime.utcnow()
        elif batch.failure_reason == 'service_restarted':
            status = 'failed'
            completed_at = datetime.utcnow()
        elif success_count == batch.requested_count and failed_count == 0:
            status = 'completed'
            completed_at = datetime.utcnow()
        elif success_count > 0:
            status = 'completed_partial'
            completed_at = datetime.utcnow()
        else:
            status = 'failed'
            completed_at = datetime.utcnow()

        failure_category = None
        if status == 'failed':
            failure_category = self._classify_failure_category(self._select_failure_reason(batch, items))

        updated = external_batch_crud.update_batch(
            db,
            batch_uuid,
            status=status,
            completed_count=completed_count,
            success_count=success_count,
            failed_count=failed_count,
            upload_success_count=upload_success_count,
            upload_failed_count=upload_failed_count,
            recent_errors=recent_errors[-20:],
            completed_at=completed_at,
            failure_category=failure_category,
        )
        db.commit()
        return self._detach_batch(db, updated)

    def ensure_failure_category(self, db: Session, batch):
        if batch is None:
            return None
        if batch.status != 'failed' or batch.failure_category:
            return batch
        items = external_batch_crud.list_batch_items(db, batch.batch_uuid)
        updated = external_batch_crud.update_batch(
            db,
            batch.batch_uuid,
            failure_category=self._classify_failure_category(self._select_failure_reason(batch, items)),
        )
        db.commit()
        return self._detach_batch(db, updated)

    async def run_batch(self, batch_uuid: str):
        from ...database.session import get_db
        from ...web.routes.registration import run_registration_task

        with get_db() as db:
            batch = external_batch_crud.update_batch(db, batch_uuid, status='running', started_at=datetime.utcnow())
            db.commit()
            runtime_snapshot = (batch.runtime_snapshot or {}).copy()
            upload_target_data = runtime_snapshot.get('upload_target')
            request_payload = batch.request_payload or {}
            mode = request_payload.get('mode', 'pipeline')
            concurrency = int(request_payload.get('concurrency', 1) or 1)
            items = external_batch_crud.list_batch_items(db, batch_uuid)
            email_service_type = batch.email_service_type

        upload_target = UploadTarget(**upload_target_data) if upload_target_data else None

        async def execute_item(item_id: int):
            with get_db() as db:
                item = db.query(ExternalRegistrationBatchItem).filter(ExternalRegistrationBatchItem.id == item_id).first()
                batch = external_batch_crud.get_batch_by_uuid(db, batch_uuid)
                if not item or not batch:
                    return
                if batch.cancel_requested:
                    external_batch_crud.update_batch_item(db, item.id, status='cancelled', upload_status='skipped', completed_at=datetime.utcnow())
                    task = crud.get_registration_task_by_uuid(db, item.registration_task_uuid)
                    if task:
                        task.status = 'cancelled'
                        task.completed_at = datetime.utcnow()
                    db.commit()
                    self.recompute_summary(db, batch_uuid)
                    return
                if item.failure_reason:
                    external_batch_crud.update_batch_item(db, item.id, status='failed', completed_at=datetime.utcnow())
                    task = crud.get_registration_task_by_uuid(db, item.registration_task_uuid)
                    if task:
                        task.status = 'failed'
                        task.error_message = item.failure_reason
                        task.completed_at = datetime.utcnow()
                    db.commit()
                    self.recompute_summary(db, batch_uuid)
                    return

                external_batch_crud.update_batch_item(db, item.id, status='running', started_at=datetime.utcnow())
                selected_service_id = item.selected_email_service_id
                db.commit()

            await run_registration_task(
                item.registration_task_uuid,
                email_service_type,
                None,
                None,
                selected_service_id,
                '[external]',
                batch_uuid,
                False,
                [],
                False,
                [],
                False,
                [],
            )

            with get_db() as db:
                item = db.query(ExternalRegistrationBatchItem).filter(ExternalRegistrationBatchItem.id == item_id).first()
                task = crud.get_registration_task_by_uuid(db, item.registration_task_uuid)
                item_status = 'completed' if task and task.status == 'completed' else ('cancelled' if task and task.status == 'cancelled' else 'failed')
                update_kwargs = {'status': item_status, 'completed_at': datetime.utcnow()}
                if item_status == 'failed':
                    update_kwargs['failure_reason'] = (task.error_message if task and task.error_message else 'registration_failed')
                external_batch_crud.update_batch_item(db, item.id, **update_kwargs)

                if item_status == 'completed' and upload_target is not None:
                    email = task.result.get('email') if task and task.result else None
                    account = None
                    if email:
                        account = db.query(Account).filter(Account.email == email).order_by(Account.id.desc()).first()
                    if account is None and task and task.result:
                        account = db.query(Account).filter(Account.account_id == task.result.get('account_id')).order_by(Account.id.desc()).first()
                    upload_result = upload_registered_account(account, upload_target)
                    external_batch_crud.update_batch_item(
                        db,
                        item.id,
                        upload_status='success' if upload_result.success else 'failed',
                        upload_error=None if upload_result.success else upload_result.message,
                    )
                db.commit()
                self.recompute_summary(db, batch_uuid)

        if mode == 'parallel':
            semaphore = asyncio.Semaphore(max(1, concurrency))

            async def guarded(item_id: int):
                async with semaphore:
                    await execute_item(item_id)

            await asyncio.gather(*(guarded(item.id) for item in items))
        else:
            for item in items:
                await execute_item(item.id)

        with get_db() as db:
            batch = external_batch_crud.get_batch_by_uuid(db, batch_uuid)
            if batch and batch.cancel_requested:
                external_batch_crud.update_batch(db, batch_uuid, status='cancelled', completed_at=datetime.utcnow())
                db.commit()
            self.recompute_summary(db, batch_uuid)

    def request_cancel(self, db: Session, batch_uuid: str):
        batch = external_batch_crud.get_batch_by_uuid(db, batch_uuid)
        if not batch:
            return None
        if batch.status not in {'pending', 'running'}:
            raise ValueError('batch is already finished')
        updated = external_batch_crud.update_batch(db, batch_uuid, cancel_requested=True)
        db.commit()
        return self._detach_batch(db, updated)


def _serialize_batch(batch) -> dict:
    return {
        'batch_uuid': batch.batch_uuid,
        'status': batch.status,
        'requested_count': batch.requested_count,
        'completed_count': batch.completed_count,
        'success_count': batch.success_count,
        'failed_count': batch.failed_count,
        'upload_success_count': batch.upload_success_count,
        'upload_failed_count': batch.upload_failed_count,
        'failure_reason': batch.failure_reason,
        'failure_category': batch.failure_category,
        'created_at': batch.created_at.isoformat() if batch.created_at else None,
        'started_at': batch.started_at.isoformat() if batch.started_at else None,
        'completed_at': batch.completed_at.isoformat() if batch.completed_at else None,
        'recent_errors': batch.recent_errors or [],
    }


def _request_from_payload(payload: dict) -> ExternalBatchCreateRequest:
    email = payload.get('email') or {}
    upload = payload.get('upload') or {}
    execution = payload.get('execution') or {}
    return ExternalBatchCreateRequest(
        count=payload['count'],
        idempotency_key=payload.get('idempotency_key'),
        email_type=email.get('type'),
        email_service_id=email.get('service_id'),
        upload_enabled=bool(upload.get('enabled')),
        upload_provider=upload.get('provider'),
        upload_service_id=upload.get('service_id'),
        mode=execution.get('mode', 'pipeline'),
        concurrency=execution.get('concurrency', 1),
        interval_min=execution.get('interval_min', 5),
        interval_max=execution.get('interval_max', 30),
    )


def create_external_registration_batch(payload: dict, background_tasks=None) -> dict:
    from ...database.session import get_db

    request = _request_from_payload(payload)
    service = ExternalBatchService()
    is_replay = False
    with get_db() as db:
        existing = external_batch_crud.get_batch_by_idempotency_key(db, request.idempotency_key) if request.idempotency_key else None
        if existing is not None:
            existing = service.ensure_failure_category(db, existing)
            response = _serialize_batch(existing)
            response['idempotent_replay'] = True
            return response
        batch = service.create_batch(db, request)
        is_replay = bool(getattr(batch, '_idempotent_replay', False))
        if is_replay:
            batch = service.ensure_failure_category(db, batch)
    if background_tasks is not None:
        background_tasks.add_task(service.run_batch, batch.batch_uuid)
    response = _serialize_batch(batch)
    response['idempotent_replay'] = is_replay
    return response


def get_external_registration_batch_status(batch_uuid: str) -> dict:
    from ...database.session import get_db

    service = ExternalBatchService()
    with get_db() as db:
        batch = external_batch_crud.get_batch_by_uuid(db, batch_uuid)
        if batch is None:
            raise ValueError('batch_not_found')
        batch = service.ensure_failure_category(db, batch)
        return _serialize_batch(batch)


def cancel_external_registration_batch(batch_uuid: str) -> dict:
    from ...database.session import get_db

    service = ExternalBatchService()
    with get_db() as db:
        batch = service.request_cancel(db, batch_uuid)
        if batch is None:
            raise ValueError('batch_not_found')
        return _serialize_batch(batch)
