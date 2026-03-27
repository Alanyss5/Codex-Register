
from __future__ import annotations

from dataclasses import dataclass

from ..database.models import Account
from .service_selection import UploadTarget
from .upload.cpa_upload import generate_token_json, upload_to_cpa
from .upload.sub2api_upload import upload_to_sub2api
from .upload.team_manager_upload import upload_to_team_manager


@dataclass
class UploadExecutionResult:
    success: bool
    provider: str
    service_id: int
    message: str


def upload_registered_account(account: Account, target: UploadTarget) -> UploadExecutionResult:
    if account is None:
        return UploadExecutionResult(False, target.provider, target.service_id, 'account_not_found')
    if not account.access_token:
        return UploadExecutionResult(False, target.provider, target.service_id, 'missing_access_token')

    if target.provider == 'cpa':
        ok, message = upload_to_cpa(
            generate_token_json(account),
            api_url=target.config_snapshot['api_url'],
            api_token=target.config_snapshot['api_token'],
        )
    elif target.provider == 'sub2api':
        ok, message = upload_to_sub2api(
            [account],
            target.config_snapshot['api_url'],
            target.config_snapshot['api_key'],
        )
    elif target.provider == 'tm':
        ok, message = upload_to_team_manager(
            account,
            target.config_snapshot['api_url'],
            target.config_snapshot['api_key'],
        )
    else:
        return UploadExecutionResult(False, target.provider, target.service_id, 'unsupported_provider')

    return UploadExecutionResult(ok, target.provider, target.service_id, message)
