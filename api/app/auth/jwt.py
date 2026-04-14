import httpx
from clerk_backend_api import Clerk
from clerk_backend_api.security.types import AuthenticateRequestOptions
from fastapi import HTTPException, Request, status

from app.settings import settings

# Module-level Clerk SDK instance — created once at startup
_clerk: Clerk | None = None


def get_clerk() -> Clerk:
    global _clerk
    if _clerk is None:
        _clerk = Clerk(bearer_auth=settings.clerk_secret_key)
    return _clerk


async def verify_request(request: Request) -> dict:
    """Verify a Clerk JWT from the incoming FastAPI request.

    Returns the token payload (claims) if valid.
    Raises HTTP 401 if the token is missing, invalid, or expired.
    """
    # Clerk SDK expects an httpx.Request — convert from FastAPI/Starlette request
    body = await request.body()
    httpx_request = httpx.Request(
        method=request.method,
        url=str(request.url),
        headers=dict(request.headers),
        content=body,
    )

    clerk = get_clerk()
    # TODO: set authorized_parties to ["https://scrapeflow.govindappa.com"] in production
    # and load from CLERK_AUTHORIZED_PARTIES env var
    request_state = clerk.authenticate_request(
        httpx_request,
        AuthenticateRequestOptions(authorized_parties=None),
    )

    if not request_state.is_signed_in or not request_state.payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unauthorized: {request_state.reason}",
        )

    return request_state.payload
