from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from app.models.job_secrets import JobSecrets, JobSecretType
from app.settings import settings


async def resolve_credentials(job: Job, db: AsyncSession) -> dict | None:
    """Resolve dispatch credentials: proxy (per-job > platform default) + cookies.

    Values are returned as Fernet ciphertext under CREDENTIALS_ENCRYPTION_KEY.
    Per-job secrets are forwarded as-is from job_secrets (already encrypted at rest
    with the same key). The platform default_proxy_url is plaintext in env — encrypted
    here before being placed in the NATS message.
    """
    result: dict = {}

    proxy_secret = await db.scalar(
        select(JobSecrets).where(
            JobSecrets.job_id == job.id,
            JobSecrets.secret_type == JobSecretType.proxy,
        )
    )
    if proxy_secret:
        result["encrypted_proxy_url"] = proxy_secret.encrypted_value
    elif settings.default_proxy_url:
        f = Fernet(settings.credentials_encryption_key)
        result["encrypted_proxy_url"] = f.encrypt(settings.default_proxy_url.encode()).decode()

    cookies_secret = await db.scalar(
        select(JobSecrets).where(
            JobSecrets.job_id == job.id,
            JobSecrets.secret_type == JobSecretType.cookies,
        )
    )
    if cookies_secret:
        result["encrypted_cookies"] = cookies_secret.encrypted_value

    return result or None
