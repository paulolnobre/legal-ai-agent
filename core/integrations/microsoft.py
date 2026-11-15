"""Signing a lawyer in to their own Microsoft account, without their password.

The device code flow exists for exactly this shape of problem: a program with
no browser needs to act for a person who has one. The agent shows a short
code, the person opens microsoft.com/devicelogin on any device (phone or the
office computer, it makes no difference), signs in there, and Microsoft hands
the agent a token. **The password never passes through Telegram, this server,
or the queue**, which is the whole reason this is the connection method and a
typed credential is not.

What the agent is given is a delegated token: it can do what that person can
do, within the scopes below, and the person or their administrator can revoke
it at any time from the Microsoft account page. Isolation between lawyers
therefore comes from Microsoft rather than from code here.

The scopes are read-only on purpose. Creating or changing a file in somebody's
drive is a decision the roadmap puts behind a proposal-and-approval flow that
does not exist yet, and a scope that is not requested cannot be misused by a
bug meanwhile.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from core.config import settings
from core.metrics import observe_external
from core.observability import traced

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

# Read the files, learn who signed in, and be given a refresh token so the
# next use does not ask again. No write scope is requested.
SCOPES = "Files.Read offline_access User.Read"

GRAPH = "https://graph.microsoft.com/v1.0"


class MicrosoftError(RuntimeError):
    """A failure worth showing somebody, never carrying a token."""


class SignInPending(MicrosoftError):
    """The person has not finished signing in yet. Not an error; a state."""


class SignInDeclined(MicrosoftError):
    """The person refused, or let the code expire."""


@dataclass(frozen=True)
class DeviceFlow:
    """What to show the person, and what to poll with afterwards."""

    user_code: str
    verification_uri: str
    device_code: str
    interval: int
    expires_in: int


@dataclass(frozen=True)
class Tokens:
    """What Microsoft gives back once the person has signed in."""

    access_token: str
    refresh_token: str


def _authority() -> str:
    """The sign-in endpoint for the account kind the firm uses.

    `common` accepts both a work account and a personal one, which is the
    right default before the firm says which it has. A tenant id here narrows
    it to that organisation, which is what a firm with Microsoft 365 will
    eventually want.
    """
    return f"https://login.microsoftonline.com/{settings.ms_tenant}/oauth2/v2.0"


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    return _client


def configured() -> bool:
    """Whether an app registration exists to sign in against."""
    return bool((settings.ms_client_id or "").strip())


async def start_device_flow() -> DeviceFlow:
    """Ask Microsoft for a code to show the person."""
    if not configured():
        raise MicrosoftError(
            "MS_CLIENT_ID is not set: there is no app registration to sign in against."
        )
    started = time.perf_counter()
    try:
        response = await _http().post(f"{_authority()}/devicecode", data={
            "client_id": settings.ms_client_id,
            "scope": SCOPES,
        })
    except httpx.HTTPError as exc:
        observe_external("microsoft", "error", time.perf_counter() - started)
        raise MicrosoftError("Microsoft did not respond.") from exc
    if response.status_code != 200:
        observe_external("microsoft", "error", time.perf_counter() - started)
        detail = _describe(response)
        raise MicrosoftError(f"Microsoft refused to start the sign-in: {detail}")
    observe_external("microsoft", "success", time.perf_counter() - started)
    flow = response.json()
    return DeviceFlow(
        user_code=str(flow.get("user_code", "")),
        verification_uri=str(flow.get("verification_uri", "https://microsoft.com/devicelogin")),
        device_code=str(flow.get("device_code", "")),
        interval=max(1, int(flow.get("interval", 5))),
        expires_in=int(flow.get("expires_in", 900)),
    )


def _describe(response: httpx.Response) -> str:
    """What went wrong, from Microsoft's own words, bounded."""
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    return str(payload.get("error_description") or payload.get("error") or
               f"HTTP {response.status_code}")[:300]


async def poll_once(device_code: str) -> Tokens:
    """One poll. Raises SignInPending while the person is still signing in."""
    try:
        response = await _http().post(f"{_authority()}/token", data={
            "client_id": settings.ms_client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        })
    except httpx.HTTPError as exc:
        raise MicrosoftError("Microsoft did not respond.") from exc

    if response.status_code == 200:
        payload = response.json()
        refresh = payload.get("refresh_token")
        if not refresh:
            # Without offline_access granted there is nothing to store, and the
            # connection would silently last one hour.
            raise MicrosoftError("Microsoft did not return a refresh token.")
        return Tokens(access_token=payload.get("access_token", ""), refresh_token=refresh)

    error = ""
    try:
        error = str(response.json().get("error", ""))
    except ValueError:
        pass
    if error in ("authorization_pending", "slow_down"):
        raise SignInPending(error)
    if error in ("authorization_declined", "expired_token", "bad_verification_code"):
        raise SignInDeclined(error)
    raise MicrosoftError(_describe(response))


@traced(name="microsoft.sign_in", run_type="tool")
async def wait_for_sign_in(flow: DeviceFlow) -> Tokens:
    """Poll until the person signs in, refuses, or the code expires.

    Microsoft dictates the interval and asks for more room with `slow_down`,
    which is honoured rather than ignored: a client that polls faster than it
    was told is the one that gets throttled.
    """
    interval = flow.interval
    deadline = time.monotonic() + flow.expires_in
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        try:
            return await poll_once(flow.device_code)
        except SignInPending as pending:
            if str(pending) == "slow_down":
                interval += 5
            continue
    raise SignInDeclined("expired_token")


async def access_token_from(refresh_token: str) -> Tokens:
    """Trade a stored refresh token for a usable access token.

    Microsoft may return a new refresh token with it, and when it does the new
    one replaces the old: a caller that stores the answer keeps working, one
    that ignores it eventually finds the connection dead for no visible reason.
    """
    started = time.perf_counter()
    try:
        response = await _http().post(f"{_authority()}/token", data={
            "client_id": settings.ms_client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": SCOPES,
        })
    except httpx.HTTPError as exc:
        observe_external("microsoft", "error", time.perf_counter() - started)
        raise MicrosoftError("Microsoft did not respond.") from exc
    if response.status_code != 200:
        observe_external("microsoft", "error", time.perf_counter() - started)
        raise SignInDeclined(_describe(response))
    observe_external("microsoft", "success", time.perf_counter() - started)
    payload = response.json()
    return Tokens(
        access_token=payload.get("access_token", ""),
        refresh_token=payload.get("refresh_token") or refresh_token,
    )


async def whoami(access_token: str) -> str:
    """The signed-in account, as a person would recognise it."""
    started = time.perf_counter()
    try:
        response = await _http().get(
            f"{GRAPH}/me", headers={"Authorization": f"Bearer {access_token}"},
        )
    except httpx.HTTPError as exc:
        observe_external("microsoft", "error", time.perf_counter() - started)
        raise MicrosoftError("Microsoft Graph did not respond.") from exc
    if response.status_code != 200:
        observe_external("microsoft", "error", time.perf_counter() - started)
        raise MicrosoftError(_describe(response))
    observe_external("microsoft", "success", time.perf_counter() - started)
    me = response.json()
    return str(me.get("userPrincipalName") or me.get("mail")
               or me.get("displayName") or "conta Microsoft")


@dataclass(frozen=True)
class DriveItem:
    """One file in somebody's drive, as a person would recognise it."""

    item_id: str
    name: str
    size: int
    modified: str
    web_url: str


def _item(raw: dict) -> DriveItem | None:
    """A file, or None for anything that is not one (a folder, a notebook)."""
    if not isinstance(raw, dict) or "file" not in raw:
        return None
    return DriveItem(
        item_id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        size=int(raw.get("size") or 0),
        modified=str(raw.get("lastModifiedDateTime", ""))[:10],
        web_url=str(raw.get("webUrl", "")),
    )


async def _graph_get(access_token: str, path: str, params: dict | None = None):
    started = time.perf_counter()
    try:
        response = await _http().get(
            f"{GRAPH}{path}", params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except httpx.HTTPError as exc:
        observe_external("microsoft", "error", time.perf_counter() - started)
        raise MicrosoftError("Microsoft Graph did not respond.") from exc
    if response.status_code == 401:
        observe_external("microsoft", "error", time.perf_counter() - started)
        raise SignInDeclined("the connection is no longer accepted")
    if response.status_code != 200:
        observe_external("microsoft", "error", time.perf_counter() - started)
        raise MicrosoftError(_describe(response))
    observe_external("microsoft", "success", time.perf_counter() - started)
    return response


@traced(name="microsoft.list_files", run_type="tool")
async def list_files(access_token: str, query: str = "", limit: int = 10) -> list[DriveItem]:
    """Files matching a search, or the most recent ones when nothing is asked.

    Folders are dropped rather than listed: this exists to bring a document
    into the archive, and a folder is not one. The cap is small on purpose,
    because fifty file names in a chat is not a listing anybody reads.
    """
    top = max(1, min(int(limit), 25))
    if query.strip():
        # Graph takes the term inside a quoted function call, so a quote in
        # the search would end it early: doubled, as OData escapes it.
        escaped = query.strip().replace("'", "''")
        response = await _graph_get(
            access_token, f"/me/drive/root/search(q='{escaped}')", {"$top": top},
        )
    else:
        response = await _graph_get(access_token, "/me/drive/recent", {"$top": top})
    items = response.json().get("value") or []
    found = [item for item in (_item(raw) for raw in items) if item is not None]
    return found[:top]


@traced(name="microsoft.download", run_type="tool")
async def download_file(access_token: str, item_id: str, *, max_bytes: int) -> bytes:
    """The file itself, refused rather than truncated when it is too large.

    Graph answers this with a redirect to short-lived storage, so redirects
    are followed here and nowhere else in this module. httpx drops the
    Authorization header when a redirect crosses to another host, which is
    what should happen: that storage URL carries its own signature and has no
    business seeing the token.
    """
    started = time.perf_counter()
    try:
        response = await _http().get(
            f"{GRAPH}/me/drive/items/{item_id}/content",
            headers={"Authorization": f"Bearer {access_token}"},
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        observe_external("microsoft", "error", time.perf_counter() - started)
        raise MicrosoftError("Microsoft Graph did not respond.") from exc
    if response.status_code == 401:
        observe_external("microsoft", "error", time.perf_counter() - started)
        raise SignInDeclined("the connection is no longer accepted")
    if response.status_code != 200:
        observe_external("microsoft", "error", time.perf_counter() - started)
        raise MicrosoftError(_describe(response))
    if len(response.content) > max_bytes:
        observe_external("microsoft", "error", time.perf_counter() - started)
        raise MicrosoftError(f"o arquivo passa do limite de {max_bytes // (1024 * 1024)} MB")
    observe_external("microsoft", "success", time.perf_counter() - started)
    return response.content


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


__all__ = [
    "DeviceFlow", "DriveItem", "MicrosoftError", "SCOPES", "SignInDeclined", "SignInPending",
    "Tokens", "access_token_from", "close", "configured", "download_file",
    "list_files", "poll_once", "start_device_flow", "wait_for_sign_in", "whoami",
]
