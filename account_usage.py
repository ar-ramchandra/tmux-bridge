"""Read-only provider quotas. Never read credential files or retain raw responses."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

POLL_SECONDS = 300
STALE_SECONDS = 900


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, allow_nan=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def number(value, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if value < 0 or (maximum is not None and value > maximum):
        return None
    return value


def text(value, limit=120):
    return ''.join(c for c in str(value or '') if c.isprintable())[:limit]


def identity(provider, account, account_id=None):
    email = text(account.get('email'))
    workspace = text(account.get('workspaceName') or account.get('orgUuid'))
    key = account_id or (email.lower() + ':' + workspace if email else '')
    label = (email.split('@')[0][:2] + '…@' + email.split('@')[-1]) if '@' in email else provider.title()
    return {'provider': provider, 'account_key': hashlib.sha256((provider+':'+key).encode()).hexdigest() if key else '',
            'label': label, 'plan': text(account.get('planType') or account.get('subscriptionType'), 60)}


def window(label, used, resets, minutes=None):
    used, resets = number(used, 100), number(resets)
    if used is None:
        return None
    return {'label': text(label, 60), 'used_percent': used, 'remaining_percent': round(100-used, 2),
            'resets_at': resets, 'window_minutes': number(minutes)}


def normalize_codex(account, limits, now=None):
    now = time.time() if now is None else now
    result = {**identity('codex', account, limits.get('accountId')), 'observed_at': now,
              'source': 'codex app-server', 'windows': [], 'status': 'ok'}
    if not account:
        return {**result, 'status': 'signed_out'}
    if account.get('type') != 'chatgpt':
        return {**result, 'status': 'unsupported'}
    buckets = limits.get('rateLimitsByLimitId') or {'codex': limits.get('rateLimits') or {}}
    for name, bucket in list(buckets.items())[:12]:
        if not isinstance(bucket, dict):
            continue
        for field in ('primary', 'secondary'):
            raw = bucket.get(field) or {}
            minutes = raw.get('windowDurationMins')
            label = 'Weekly' if minutes == 10080 else '5 hours' if minutes == 300 else (str(minutes)+' minutes' if number(minutes) else field.title())
            if name != 'codex':
                label = text(bucket.get('limitName') or name, 30) + ' · ' + label
            item = window(label, raw.get('usedPercent'), raw.get('resetsAt'), minutes)
            if item:
                result['windows'].append(item)
    if not result['windows']:
        result['status'] = 'unavailable'
    return result


def normalize_claude(data, account, now=None):
    result = {**identity('claude', account), 'observed_at': time.time() if now is None else now,
              'source': 'claude status line', 'windows': [], 'status': 'unavailable'}
    rates = data.get('rate_limits') or {}
    for name, label, minutes in [('five_hour', '5 hours', 300), ('seven_day', 'Weekly', 10080), ('spend_limit', 'Spend limit', None)]:
        raw = rates.get(name) or {}
        item = window(label, raw.get('used_percentage'), raw.get('resets_at'), minutes)
        if item:
            result['windows'].append(item)
    if result['windows']:
        result['status'] = 'ok'
    return result


def find_cli(name):
    override = os.environ.get('TMUX_BRIDGE_'+name.upper()+'_BIN')
    if override:
        return override if Path(override).is_file() else None
    candidates = [shutil.which(name), str(Path.home()/'.local/bin'/name), '/opt/homebrew/bin/'+name]
    candidates += [str(p) for p in sorted((Path.home()/'.nvm/versions/node').glob('*/bin/'+name), reverse=True)]
    return next((p for p in candidates if p and Path(p).is_file()), None)


def cli_env(executable):
    env = os.environ.copy()
    env['PATH'] = str(Path(executable).parent) + os.pathsep + env.get('PATH', '')
    return env


def codex_read(executable, timeout=15):
    # A dedicated stdio server only reads account metadata. It does not create
    # threads, run tools, attach to terminals, or change the shared daemon.
    process = subprocess.Popen([executable, 'app-server', '--stdio'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True, env=cli_env(executable))
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer, results = b'', {}
    def send(value):
        process.stdin.write(json.dumps(value).encode()+b'\n')
        process.stdin.flush()
    try:
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'tmux_bridge_usage', 'version': '0.1.0'}}})
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if not selector.select(min(.5, max(0, deadline-time.monotonic()))):
                continue
            raw = os.read(process.stdout.fileno(), 65536)
            if not raw:
                break
            buffer += raw
            if len(buffer) > 1024*1024:
                raise ValueError('Oversize provider response')
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                response = json.loads(line)
                identifier = response.get('id')
                if identifier not in (1, 2, 3):
                    continue
                if 'error' in response and identifier == 1:
                    raise ValueError('Provider account read failed')
                if identifier == 1:
                    send({'method': 'initialized', 'params': {}})
                    send({'id': 2, 'method': 'account/read', 'params': {'refreshToken': False}})
                    send({'id': 3, 'method': 'account/rateLimits/read', 'params': {}})
                else:
                    results[identifier] = response.get('result') if 'result' in response else None
            if 2 in results and 3 in results:
                if results[2] is None:
                    raise ValueError('Provider account read failed')
                return normalize_codex(results[2].get('account') or {}, results[3] or {})
        raise TimeoutError('Provider account read timed out')
    finally:
        selector.close()
        # Only terminate our private probe, never the user's shared daemon.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        for stream in (process.stdin, process.stdout):
            stream.close()


def claude_auth(executable):
    response = subprocess.run([executable, 'auth', 'status', '--json'], capture_output=True,
                              timeout=2, env=cli_env(executable))
    if len(response.stdout) > 65536:
        raise ValueError('Oversize account response')
    raw = json.loads(response.stdout)
    # Explicit metadata allowlist; never persist the raw CLI response.
    return {key: raw.get(key) for key in ('loggedIn', 'authMethod', 'email', 'orgUuid', 'subscriptionType')}


def collect(root):
    root = Path(root)
    accounts = []
    codex = find_cli('codex')
    if codex:
        try:
            accounts.append(codex_read(codex))
        except Exception:
            accounts.append({'provider': 'codex', 'label': 'Codex', 'status': 'unavailable', 'windows': [], 'observed_at': None})
    claude = find_cli('claude')
    if claude:
        reports = sorted((root/'claude-usage').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:64]
        by_account = {}
        for path in reports:
            try:
                value = json.loads(path.read_text())
                if time.time()-value['observed_at'] > 86400:
                    continue
                key = value.get('account_key') or path.stem
                if key not in by_account:
                    by_account[key] = value
            except (OSError, ValueError, KeyError, TypeError):
                continue
        accounts.extend(by_account.values())
        if not by_account:
            try:
                auth = claude_auth(claude)
                status = 'awaiting_report' if auth.get('loggedIn') else 'signed_out'
                if auth.get('authMethod') in ('api_key', 'apiKey'):
                    status = 'unsupported'
                accounts.append({**identity('claude', auth), 'status': status, 'windows': [], 'observed_at': None})
            except Exception:
                accounts.append({'provider': 'claude', 'label': 'Claude', 'status': 'unavailable', 'windows': [], 'observed_at': None})
    return {'collected_at': time.time(), 'accounts': accounts[:64]}


def sanitized_snapshot(raw):
    """Validate the node boundary and strip unknown fields before persistence."""
    if not isinstance(raw, dict) or not isinstance(raw.get('accounts'), list) or len(raw['accounts']) > 64:
        raise ValueError('Invalid account snapshot')
    result = {'collected_at': number(raw.get('collected_at')), 'accounts': []}
    now = time.time()
    for account in raw['accounts']:
        provider = account.get('provider')
        if provider not in ('codex', 'claude'):
            continue
        key = account.get('account_key', '')
        if key and (len(key) != 64 or any(c not in '0123456789abcdef' for c in key)):
            raise ValueError('Invalid account identity')
        status = account.get('status')
        if status not in ('ok', 'unavailable', 'signed_out', 'unsupported', 'awaiting_report'):
            raise ValueError('Invalid account state')
        observed = number(account.get('observed_at'))
        if observed and observed > now+60:
            raise ValueError('Future account observation')
        clean = {'provider': provider, 'account_key': key, 'label': text(account.get('label')),
                 'plan': text(account.get('plan'), 60), 'source': text(account.get('source'), 60),
                 'status': status, 'observed_at': observed, 'windows': []}
        for value in account.get('windows', [])[:24]:
            item = window(value.get('label'), value.get('used_percent'), value.get('resets_at'), value.get('window_minutes'))
            if item:
                clean['windows'].append(item)
        result['accounts'].append(clean)
    return result


def ensure_table(store):
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS account_usage (server TEXT PRIMARY KEY, body TEXT NOT NULL, received REAL NOT NULL)')


def store_snapshot(store, server, raw):
    value = sanitized_snapshot(raw)
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO account_usage VALUES (?,?,?)', (server, json.dumps(value), time.time()))


def snapshots(store):
    with store.connect() as db:
        try:
            rows = db.execute('SELECT * FROM account_usage ORDER BY server').fetchall()
        except sqlite3.OperationalError:
            return []
    result = []
    now = time.time()
    for row in rows:
        value = json.loads(row['body'])
        for account in value['accounts']:
            observed = account['observed_at']
            account['stale'] = observed is None or now-observed > STALE_SECONDS or now-row['received'] > STALE_SECONDS
            for item in account['windows']:
                item['reset_passed'] = item['resets_at'] is not None and item['resets_at'] <= now
        result.append({'server': row['server'], 'received_at': row['received'], **value})
    return result


def install_statusline(root, home=None):
    root, home = Path(root).resolve(), Path(home or Path.home())
    settings = home/'.claude/settings.json'
    current = json.loads(settings.read_text()) if settings.exists() else {}
    old = current.get('statusLine')
    command = shlex.join([sys.executable, str(Path(__file__).absolute()), '--agent-dir', str(root), 'claude-statusline'])
    if old and 'account_usage.py' in old.get('command', '') and 'claude-statusline' in old.get('command', ''):
        current['statusLine'] = {**old, 'command': command}
    else:
        private_json(root/'claude-statusline-original.json', old)
        if settings.exists():
            backup = settings.with_name(settings.name+'.before-usage-'+str(time.time_ns()))
            shutil.copy2(settings, backup)
            backup.chmod(0o600)
        current['statusLine'] = {**(old or {}), 'type': 'command', 'command': command}
    private_json(settings, current)
    return settings


def record_statusline(root, raw):
    """Runs in Claude's environment; preserve any preexisting status renderer."""
    root = Path(root)
    try:
        data = json.loads(raw)
        session = hashlib.sha256(str(data.get('session_id') or 'default').encode()).hexdigest()
        directory = root/'claude-usage'
        auth_path = root/('claude-account-'+session+'.json')
        account = {}
        if auth_path.exists() and time.time()-auth_path.stat().st_mtime < 300:
            account = json.loads(auth_path.read_text())
        else:
            executable = find_cli('claude')
            if executable:
                try:
                    account = claude_auth(executable)
                    private_json(auth_path, account)
                except Exception:
                    pass  # Retain valid quota observations without an identity.
        private_json(directory/(session+'.json'), normalize_claude(data, account))
        # Bound inactive session files and their account-metadata caches.
        for old in sorted(directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[64:]:
            old.unlink(missing_ok=True)
            (root/('claude-account-'+old.stem+'.json')).unlink(missing_ok=True)
    except Exception:
        pass  # A missing usage sample must never interfere with Claude's UI.
    original = root/'claude-statusline-original.json'
    if original.exists():
        try:
            setting = json.loads(original.read_text())
        except (ValueError, OSError):
            return
        if setting and setting.get('type') == 'command' and setting.get('command'):
            try:
                result = subprocess.run(setting['command'], shell=True, input=raw, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, timeout=5)
                sys.stdout.buffer.write(result.stdout)
            except (OSError, subprocess.TimeoutExpired):
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent-dir', type=Path, default=Path.home()/'.tmux-bridge')
    parser.add_argument('action', choices=['install-claude', 'claude-statusline', 'collect'])
    args = parser.parse_args()
    if args.action == 'install-claude':
        print(install_statusline(args.agent_dir))
    elif args.action == 'claude-statusline':
        raw = sys.stdin.buffer.read(1024*1024+1)
        if len(raw) <= 1024*1024:
            record_statusline(args.agent_dir, raw)
    else:
        print(json.dumps(sanitized_snapshot(collect(args.agent_dir))))


if __name__ == '__main__':
    main()
