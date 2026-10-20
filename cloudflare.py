"""
Cloudflare API operations for tmux-bridge.
Extracted from gateway.py — handles tunnel, DNS, Access setup/teardown.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import base64
from pathlib import Path

import httpx

CONFIG_FILE = Path(os.environ.get("TMUX_BRIDGE_CONFIG_DIR", Path.home() / ".config/tmux-bridge")).expanduser() / "gateway.json"

_cloudflared_proc = None


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save_config(data: dict):
    CONFIG_FILE.parent.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))
    CONFIG_FILE.chmod(0o600)


async def validate_token(token: str) -> tuple[bool, str]:
    """Validate CF API token. Returns (success, message)."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            "https://api.cloudflare.com/client/v4/user/tokens/verify",
            headers={"Authorization": f"Bearer {token}"},
        )
        d = r.json()
        if d.get("success"):
            return True, "API token valid"
        err = (d.get("errors") or [{}])[0].get("message", "Check token permissions")
        return False, err


async def get_account_and_email(token: str) -> tuple[str, str, str]:
    """Get account ID, account name, and user email. Returns (account_id, name, email)."""
    headers = {"Authorization": f"Bearer {token}"}
    account_id = ""
    acct_name = ""
    email = ""

    async with httpx.AsyncClient(timeout=20) as client:
        # Get accounts
        r = await client.get(
            "https://api.cloudflare.com/client/v4/accounts?page=1&per_page=5",
            headers=headers,
        )
        accounts = r.json().get("result", [])
        if accounts:
            account_id = accounts[0]["id"]
            acct_name = accounts[0].get("name", "Account found")

        # Get email
        r = await client.get(
            "https://api.cloudflare.com/client/v4/user",
            headers=headers,
        )
        d = r.json()
        if d.get("success") and d.get("result", {}).get("email"):
            email = d["result"]["email"]

    return account_id, acct_name, email


async def find_zone(token: str, domain: str = "") -> tuple[str, str, str, str]:
    """
    Find DNS zone. Returns (zone_id, zone_name, domain, account_id_from_zone).
    If domain is given, looks for matching zone. Otherwise uses first available.
    """
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            "https://api.cloudflare.com/client/v4/zones?status=active&per_page=5",
            headers=headers,
        )
        zones = r.json().get("result", [])

        if domain:
            parts = domain.split(".")
            zone_name = ".".join(parts[-2:]) if len(parts) >= 2 else domain
            matched = [z for z in zones if z["name"] == zone_name]
            if matched:
                acct_id = matched[0].get("account", {}).get("id", "")
                return matched[0]["id"], matched[0]["name"], domain, acct_id
            raise ValueError(f"No zone found for {zone_name}")
        elif zones:
            z = zones[0]
            acct_id = z.get("account", {}).get("id", "")
            return z["id"], z["name"], f"bridge.{z['name']}", acct_id
        else:
            return "", "", "", ""


async def create_or_reuse_tunnel(token: str, account_id: str) -> tuple[str, str]:
    """Create or reuse tmux-bridge tunnel. Returns (tunnel_id, tunnel_token)."""
    headers = {"Authorization": f"Bearer {token}"}
    tunnel_name = "tmux-bridge"

    async with httpx.AsyncClient(timeout=20) as client:
        # Check for existing
        r = await client.get(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/cfd_tunnel"
            f"?name={tunnel_name}&is_deleted=false",
            headers=headers,
        )
        existing = r.json().get("result", [])

        if existing:
            tunnel_id = existing[0]["id"]
            # Clean up duplicates
            for dup in existing[1:]:
                try:
                    await client.delete(
                        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
                        f"/cfd_tunnel/{dup['id']}",
                        headers={**headers, "Content-Type": "application/json"},
                        json={"cascade": True},
                    )
                except Exception:
                    pass
        else:
            secret = base64.b64encode(secrets.token_bytes(32)).decode()
            r = await client.post(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/cfd_tunnel",
                headers={**headers, "Content-Type": "application/json"},
                json={"name": tunnel_name, "tunnel_secret": secret, "config_src": "cloudflare"},
            )
            d = r.json()
            if not d.get("success"):
                err = (d.get("errors") or [{}])[0].get("message", "Unknown error")
                raise RuntimeError(f"Tunnel creation failed: {err}")
            tunnel_id = d["result"]["id"]

        # Get tunnel run token
        tunnel_token = ""
        r = await client.get(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/cfd_tunnel/{tunnel_id}/token",
            headers=headers,
        )
        d = r.json()
        if d.get("success"):
            tunnel_token = d.get("result", "")

    return tunnel_id, tunnel_token


async def configure_ingress(token: str, account_id: str, tunnel_id: str, domain: str,
                            *, team_name: str, audience: str, port: int = 4601):
    """Publish the browser application only with mandatory Access validation.

    Agents retain their private connection. Never bypass browser authentication
    to make machine endpoints work on this hostname.
    """
    if not team_name or not audience or not domain or not 1 <= port <= 65535:
        raise ValueError("Domain, Access team/audience and a valid gateway port are required")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.put(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/cfd_tunnel/{tunnel_id}/configurations",
            headers=headers,
            json={"config": {"ingress": [
                {"hostname": domain, "service": f"http://127.0.0.1:{port}",
                 "originRequest": {"httpHostHeader": domain, "access": {
                     "required": True, "teamName": team_name, "audTag": [audience],
                 }}},
                {"service": "http_status:404"},
            ]}},
        )
        if r.is_error or not r.json().get("success"):
            raise RuntimeError("Cloudflare rejected the protected tunnel configuration")


async def setup_dns(token: str, zone_id: str, domain: str, tunnel_id: str):
    """Create or update CNAME record pointing to tunnel."""
    headers = {"Authorization": f"Bearer {token}"}
    cname = f"{tunnel_id}.cfargotunnel.com"

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}"
            f"/dns_records?type=CNAME&name={domain}",
            headers=headers,
        )
        existing = r.json().get("result", [])

        if existing:
            r = await client.put(
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}"
                f"/dns_records/{existing[0]['id']}",
                headers={**headers, "Content-Type": "application/json"},
                json={"type": "CNAME", "name": domain, "content": cname, "proxied": True},
            )
        else:
            r = await client.post(
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
                headers={**headers, "Content-Type": "application/json"},
                json={"type": "CNAME", "name": domain, "content": cname, "proxied": True},
            )

        d = r.json()
        if not d.get("success"):
            err = (d.get("errors") or [{}])[0].get("message", "Unknown error")
            raise RuntimeError(f"DNS setup failed: {err}")


async def setup_access(token: str, account_id: str, domain: str, email: str) -> dict:
    """Create an email-restricted browser app; return its ID and JWT audience.

    Existing applications require policy review rather than silently assuming
    they are protected. Configure the email OTP identity provider separately.
    """
    if not domain or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("A hostname and an exact email address are required")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/access/apps",
            headers=headers,
            json={"name": "Terminal Bridge", "domain": domain,
                  "type": "self_hosted", "session_duration": "8h"},
        )
        d = r.json()
        if r.is_error or not d.get("success"):
            raise RuntimeError("Access app creation failed; review any existing application and its policies")
        app = d["result"]
        if not app.get("id") or not app.get("aud"):
            raise RuntimeError("Access application is missing its ID or JWT audience")
        r = await client.post(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/access/apps/{app['id']}/policies",
            headers=headers,
            json={"name": "Admin", "decision": "allow",
                  "include": [{"email": {"email": email}}]},
        )
        if r.is_error or not r.json().get("success"):
            # Leave the app deny-by-default. Do not publish its tunnel or DNS.
            raise RuntimeError("Email access policy creation failed; tunnel must remain unpublished")
        return {"id": app["id"], "aud": app["aud"]}


async def teardown_all(token: str, account_id: str, zone_id: str,
                       tunnel_id: str, domain: str):
    """Full teardown: delete tunnel, DNS record, Access app."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=20) as client:
        # Stop cloudflared first
        kill_cloudflared()

        # Delete Access app
        try:
            r = await client.get(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/access/apps",
                headers=headers,
            )
            for app in r.json().get("result", []):
                if app.get("domain") == domain or app.get("name") == "Terminal Bridge":
                    await client.delete(
                        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
                        f"/access/apps/{app['id']}",
                        headers=headers,
                    )
        except Exception:
            pass

        # Delete DNS record
        if zone_id and domain:
            try:
                r = await client.get(
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}"
                    f"/dns_records?type=CNAME&name={domain}",
                    headers=headers,
                )
                for rec in r.json().get("result", []):
                    await client.delete(
                        f"https://api.cloudflare.com/client/v4/zones/{zone_id}"
                        f"/dns_records/{rec['id']}",
                        headers=headers,
                    )
            except Exception:
                pass

        # Delete tunnel
        if tunnel_id:
            try:
                # Clean up connections first
                await client.delete(
                    f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
                    f"/cfd_tunnel/{tunnel_id}/connections",
                    headers=headers,
                )
                await asyncio.sleep(2)
                await client.delete(
                    f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
                    f"/cfd_tunnel/{tunnel_id}",
                    headers=headers,
                    json={"cascade": True},
                )
            except Exception:
                pass


# ── Cloudflared process management ──

def kill_cloudflared():
    """Stop only the tunnel process started by this gateway."""
    global _cloudflared_proc
    if _cloudflared_proc:
        try:
            _cloudflared_proc.terminate()
            _cloudflared_proc.wait(timeout=5)
        except Exception:
            try:
                _cloudflared_proc.kill()
            except Exception:
                pass
        _cloudflared_proc = None


async def start_cloudflared(config: dict) -> str | None:
    """Start an explicitly configured named tunnel; never fall back to public access."""
    global _cloudflared_proc
    if config.get("quick_tunnel") or not all(config.get(k) for k in (
        "cf_tunnel_token", "domain", "cf_access_team", "cf_access_aud",
    )):
        raise ValueError("A named tunnel with configured Cloudflare Access is required")
    kill_cloudflared()
    # Keep the tunnel secret out of process command-line arguments.
    env = {**os.environ, "TUNNEL_TOKEN": config["cf_tunnel_token"]}
    _cloudflared_proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--config", "/dev/null", "run"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(3)
    if _cloudflared_proc.poll() is not None:
        _cloudflared_proc = None
        return None
    return config["domain"]


def is_tunnel_running() -> bool:
    return _cloudflared_proc is not None and _cloudflared_proc.poll() is None
