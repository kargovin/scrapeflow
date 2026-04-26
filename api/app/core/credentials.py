import json

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from app.models.job_secrets import JobSecrets, JobSecretType
from app.settings import settings


async def resolve_credentials(job: Job, db: AsyncSession) -> dict | None:
    """Resolve dispatch credentials: proxy (per-job > platform default) + cookies."""
    result: dict = {}

    proxy_secret = await db.scalar(
        select(JobSecrets).where(
            JobSecrets.job_id == job.id,
            JobSecrets.secret_type == JobSecretType.proxy,
        )
    )
    if proxy_secret:
        f = Fernet(settings.llm_key_encryption_key)
        result["proxy_url"] = f.decrypt(proxy_secret.encrypted_value.encode()).decode()
    elif settings.default_proxy_url:
        result["proxy_url"] = settings.default_proxy_url

    cookies_secret = await db.scalar(
        select(JobSecrets).where(
            JobSecrets.job_id == job.id,
            JobSecrets.secret_type == JobSecretType.cookies,
        )
    )
    if cookies_secret:
        f = Fernet(settings.llm_key_encryption_key)
        result["cookies"] = json.loads(f.decrypt(cookies_secret.encrypted_value.encode()).decode())

    return result or None
