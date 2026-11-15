"""Spike: read a personal OneDrive through Microsoft Graph, and nothing else.

This is an experiment for the roadmap's viability gate, not part of the product.
It is deliberately standalone: no database, no queue, no agent, no writes. Run
it, look at what Graph actually returns for this account, and decide from
evidence whether the integration is worth building.

**A personal account forces the design.** ``@hotmail.com`` is a Microsoft
account, not a directory identity, so it has no tenant — and application
permissions (client credentials, "app-only") require one. The only way in is
delegated: a human signs in and the app acts as them. On a machine with no
browser that means the device code flow, which is what this uses.

Scopes are the smallest that answer the question: read the files, learn who
signed in, and keep a refresh token so the next run does not ask again.
Nothing here can modify a file.

Production would use ``msal``, which handles the token cache and refresh
correctly. This talks to the endpoints directly to keep the spike free of a
dependency the project may never adopt.

Setup, once, at https://entra.microsoft.com → App registrations → New:

1. Supported account types: **Personal Microsoft accounts only**.
2. No redirect URI is needed, but under Authentication → Advanced settings,
   **enable "Allow public client flows"**. Device code fails with a confusing
   error when this is off, and it is the step most often missed.
3. Copy the Application (client) ID — "ID do aplicativo (cliente)" in the
   Portuguese portal — into ``MS_CLIENT_ID``, in ``.env`` or inline. It is not
   the tenant id ("ID do Locatário"), which a personal account does not have
   and this flow never asks for.

    python clients/demo_firm/spike_onedrive.py
"""
import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

# "consumers" is the personal-account authority. Using "common" would also
# accept work accounts, which this spike is deliberately not testing.
_AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
_GRAPH = "https://graph.microsoft.com/v1.0"

# Read files, identify the signer, and be given a refresh token. No write scope
# is requested, so a mistake in this spike cannot alter the drive.
_SCOPES = "Files.Read offline_access User.Read"

# Spike-only. A refresh token is a long-lived credential: this keeps it out of
# git and off other accounts on the machine, but it is not the production
# design, which the roadmap says must define storage and encryption explicitly.
_TOKEN_CACHE = _ROOT / ".ms_token_cache.json"


class SpikeError(RuntimeError):
    """A failure worth reading, rather than a traceback."""


def _client_id() -> str:
    """The Application (client) ID — not the tenant, which this flow never uses.

    Read from the environment, falling back to .env, because that is where
    every other credential in this project already lives and reaching for it
    there is the natural instinct.
    """
    client_id = (os.environ.get("MS_CLIENT_ID") or "").strip()
    if not client_id:
        try:
            for line in (_ROOT / ".env").read_text(encoding="utf-8").splitlines():
                name, _, value = line.partition("=")
                if name.strip() == "MS_CLIENT_ID":
                    client_id = value.strip().strip("\"'")
                    break
        except OSError:
            pass
    if not client_id:
        raise SpikeError(
            "MS_CLIENT_ID is not set. Register an app (see the module docstring), "
            "then put its Application (client) ID ('ID do aplicativo (cliente)', "
            "not 'ID do Locatário') in .env as MS_CLIENT_ID, or pass it inline."
        )
    return client_id


def _save_refresh_token(token: str) -> None:
    _TOKEN_CACHE.write_text(json.dumps({"refresh_token": token}), encoding="utf-8")
    _TOKEN_CACHE.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600


def _load_refresh_token() -> str | None:
    try:
        return json.loads(_TOKEN_CACHE.read_text(encoding="utf-8")).get("refresh_token")
    except (OSError, ValueError):
        return None


async def _redeem_refresh_token(client: httpx.AsyncClient, client_id: str) -> str | None:
    """Reuse the previous sign-in, so a second run does not ask again."""
    refresh_token = _load_refresh_token()
    if not refresh_token:
        return None
    response = await client.post(f"{_AUTHORITY}/token", data={
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": _SCOPES,
    })
    if response.status_code != 200:
        # An expired or revoked token is not an error worth stopping for; the
        # device code flow below simply asks the human again.
        return None
    payload = response.json()
    if payload.get("refresh_token"):
        _save_refresh_token(payload["refresh_token"])
    return payload.get("access_token")


async def _sign_in(client: httpx.AsyncClient, client_id: str) -> str:
    """Device code flow: show a code, wait for the human, return a token."""
    started = await client.post(f"{_AUTHORITY}/devicecode", data={
        "client_id": client_id,
        "scope": _SCOPES,
    })
    if started.status_code != 200:
        detail = started.json().get("error_description", started.text)[:300]
        raise SpikeError(f"Microsoft refused to start the sign-in: {detail}")
    flow = started.json()

    print("\n" + "=" * 68)
    print(f"  Abra:   {flow['verification_uri']}")
    print(f"  Código: {flow['user_code']}")
    print(f"  Entre com a conta pessoal. Expira em {flow['expires_in'] // 60} min.")
    print("=" * 68 + "\n")

    interval = int(flow.get("interval", 5))
    deadline = time.monotonic() + int(flow.get("expires_in", 900))
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        response = await client.post(f"{_AUTHORITY}/token", data={
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": flow["device_code"],
        })
        if response.status_code == 200:
            payload = response.json()
            if payload.get("refresh_token"):
                _save_refresh_token(payload["refresh_token"])
            return payload["access_token"]

        error = response.json().get("error", "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "authorization_declined":
            raise SpikeError("Sign-in was declined in the browser.")
        if error == "expired_token":
            break
        detail = response.json().get("error_description", "")[:300]
        raise SpikeError(f"Sign-in failed ({error}): {detail}")
    raise SpikeError("The sign-in code expired before it was used.")


async def _graph(client: httpx.AsyncClient, token: str, path: str) -> dict:
    response = await client.get(
        f"{_GRAPH}{path}", headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code == 403:
        raise SpikeError(
            f"Graph refused {path}: the app registration is missing a permission "
            "or consent was not granted."
        )
    if response.status_code != 200:
        detail = response.text[:300]
        raise SpikeError(f"Graph returned HTTP {response.status_code} for {path}: {detail}")
    return response.json()


def _describe(item: dict) -> str:
    if "folder" in item:
        return f"  [pasta]   {item.get('name')}  ({item['folder'].get('childCount', 0)} itens)"
    size = int(item.get("size") or 0)
    return f"  [arquivo] {item.get('name')}  ({size / 1024:.0f} KB)"


async def _main() -> None:
    client_id = _client_id()
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _redeem_refresh_token(client, client_id)
        if token:
            print("Sessão anterior reaproveitada, sem novo login.")
        else:
            token = await _sign_in(client, client_id)

        me = await _graph(client, token, "/me")
        print(f"\nConectado como: {me.get('displayName')} <{me.get('userPrincipalName')}>")

        drive = await _graph(client, token, "/me/drive")
        quota = drive.get("quota") or {}
        used = int(quota.get("used") or 0) / 1024**3
        total = int(quota.get("total") or 0) / 1024**3
        print(f"Drive: {drive.get('driveType')}: {used:.1f} GB de {total:.1f} GB em uso\n")

        root = await _graph(client, token, "/me/drive/root/children?$top=25")
        items = root.get("value", [])
        if not items:
            print("A raiz do OneDrive está vazia.")
            return
        print(f"Raiz do OneDrive ({len(items)} itens):")
        for item in items:
            print(_describe(item))

        documents = [i for i in items if "file" in i]
        if documents:
            print(
                f"\n{len(documents)} arquivo(s) legível(is). O próximo spike baixaria "
                "um deles e passaria pela mesma extração de texto do acervo."
            )


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except SpikeError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        raise SystemExit(1) from exc
