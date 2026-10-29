"""Provider adapters and additive hook installation. Hooks never control agents."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

from activity import ActivityStore, new_event

HOOKS = {
    'codex': ['SessionStart','UserPromptSubmit','PermissionRequest','Stop','Interrupt','SessionEnd'],
    'claude': ['SessionStart','UserPromptSubmit','PermissionRequest','Notification','Stop','StopFailure','TaskCompleted','SessionEnd'],
}


def normalize(provider, data):
    if not isinstance(data, dict):
        raise ValueError('Expected an object')
    thread = data.get('session_id') or data.get('thread-id') or data.get('thread_id')
    event = data.get('hook_event_name') or data.get('type')
    turn = data.get('turn_id') or data.get('turn-id') or ''
    if provider in ('codex','claude'):
        kind = {'SessionStart':'idle','UserPromptSubmit':'running','PermissionRequest':'waiting',
                'Stop':'turn_finished','agent-turn-complete':'turn_finished','StopFailure':'failed',
                'Interrupt':'interrupted','TaskCompleted':'task_completed','SessionEnd':'stopped'}.get(event)
        if event == 'Notification':
            kind = {'permission_prompt':'waiting','elicitation_dialog':'waiting','idle_prompt':'idle'}.get(data.get('notification_type'))
        # SubagentStop is deliberately not mapped to parent completion.
    elif provider == 'opencode':
        props = data.get('properties', {})
        thread = props.get('sessionID') or props.get('sessionId') or thread
        turn = props.get('executionID') or props.get('executionId') or turn
        kind = {'session.idle':'idle','session.execution.started':'running',
                'session.execution.succeeded':'turn_finished','session.execution.failed':'failed',
                'session.execution.interrupted':'interrupted','session.error':'failed',
                'permission.asked':'waiting','permission.updated':'waiting'}.get(event)
        if event == 'session.status':
            kind = {'busy':'running','retry':'running','idle':'idle'}.get(props.get('status',{}).get('type'))
    else:
        raise ValueError('Unknown provider')
    if not kind or not thread:
        return None
    result = new_event(provider, thread, kind, turn_id=turn)
    # Stable native turn IDs deduplicate hook retries. Events without one stay
    # distinct; idle remains idle and never claims successful completion.
    native = data.get('task_id') if event == 'TaskCompleted' else turn
    if native:
        result['id'] = hashlib.sha256(json.dumps([provider,thread,event,native]).encode()).hexdigest()
    return result


def detect_session():
    pane = os.environ.get('TMUX_PANE', '')
    if not pane.startswith('%') or not pane[1:].isdigit():
        return ''
    try:
        result = subprocess.run(['tmux','display-message','-p','-t',pane,'#{session_name}'],
                                capture_output=True,text=True,timeout=1)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError,subprocess.TimeoutExpired):
        return ''


def record(provider, data, root):
    event = normalize(provider, data)
    if event is None:
        return None
    store = ActivityStore(Path(root)/'activity.sqlite3')
    event['session'] = store.binding(provider,event['thread_id']) or detect_session()
    if event['session']:
        store.bind(provider,event['thread_id'],event['session'])
    return store.enqueue(store.correlate(event,new_turn=data.get('hook_event_name')=='UserPromptSubmit'))


def hook_config(provider, root, executable=None):
    command = shlex.join([executable or sys.executable,str(Path(__file__).resolve()),
                          '--agent-dir',str(Path(root).expanduser().resolve()),'hook',provider])
    return {'hooks': {event:[{'hooks':[{'type':'command','command':command,'timeout':3}]}]
                      for event in HOOKS[provider]}}


def install_hooks(provider, root, home=None):
    home = Path(home or Path.home())
    target = home/('.codex/hooks.json' if provider == 'codex' else '.claude/settings.json')
    current = json.loads(target.read_text()) if target.exists() else {}
    desired = hook_config(provider, root)
    hooks = current.setdefault('hooks',{})
    for event, groups in desired['hooks'].items():
        existing = hooks.setdefault(event,[])
        for group in groups:
            if group not in existing:
                existing.append(group)
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if target.exists():
        shutil.copy2(target,target.with_name(target.name+'.before-tmux-bridge-'+str(time.time_ns())))
    temp = target.with_name(target.name+'.tmp-'+str(os.getpid()))
    fd = os.open(str(temp),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(current,f,indent=2);f.write('\n')
    os.replace(temp,target)
    return target


def opencode_plugin(root, version=1):
    command = json.dumps([sys.executable,str(Path(__file__).resolve()),'--agent-dir',str(Path(root).resolve()),'hook','opencode'])
    shared = '''import { spawn } from "node:child_process";
const command = COMMAND;
const allowed = new Set(["session.idle","session.status","session.error","session.execution.started","session.execution.succeeded","session.execution.failed","session.execution.interrupted","permission.asked","permission.updated"]);
function report(event) {
  if (!allowed.has(event.type)) return;
  const props = event.properties || {};
  const payload = {type:event.type,properties:{sessionID:props.sessionID || props.sessionId,executionID:props.executionID || props.executionId,status:props.status}};
  const child = spawn(command[0],command.slice(1),{stdio:["pipe","ignore","ignore"],timeout:2500});
  child.on("error",()=>{}); child.stdin.on("error",()=>{});
  child.stdin.end(JSON.stringify(payload));
}
'''.replace('COMMAND',command)
    if version == 1:
        return shared+'export const TmuxBridgeActivity = async () => ({event: async ({event}) => report(event)});\n'
    return 'import { Plugin } from "@opencode/plugin";\n'+shared+'''export default Plugin.define({
 id:"tmux-bridge-activity", setup(ctx) {
  const controller=new AbortController();
  void (async()=>{try {for await(const event of ctx.event.subscribe({signal:controller.signal})) report(event);} catch(error) {if(!controller.signal.aborted) console.error("tmux-bridge event subscription ended");}})();
  return ()=>controller.abort();
 }
});
'''


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent-dir',default=os.environ.get('TMUX_BRIDGE_AGENT_DIR',str(Path.home()/'.tmux-bridge')))
    commands=parser.add_subparsers(dest='action',required=True)
    hook=commands.add_parser('hook');hook.add_argument('provider',choices=['codex','claude','opencode'])
    install=commands.add_parser('install');install.add_argument('provider',choices=list(HOOKS))
    config=commands.add_parser('config');config.add_argument('provider',choices=list(HOOKS))
    plugin=commands.add_parser('opencode-plugin');plugin.add_argument('--version',type=int,choices=[1,2],default=1)
    bind=commands.add_parser('bind');bind.add_argument('provider');bind.add_argument('thread_id');bind.add_argument('session')
    args=parser.parse_args(argv)
    if args.action=='hook':
        try:
            raw=sys.stdin.buffer.read(1024*1024+1)
            if len(raw)>1024*1024:raise ValueError('Oversize hook')
            record(args.provider,json.loads(raw),args.agent_dir)
        except Exception as error:
            print('tmux-bridge activity not recorded: '+type(error).__name__,file=sys.stderr)
        print('{}')  # Never block, interrupt, or provide instructions to the agent.
    elif args.action=='install':
        print(install_hooks(args.provider,args.agent_dir))
        if args.provider=='codex':print('Review and trust these hooks using /hooks in Codex. Existing trust is not modified.')
    elif args.action=='config':print(json.dumps(hook_config(args.provider,args.agent_dir),indent=2))
    elif args.action=='opencode-plugin':print(opencode_plugin(args.agent_dir,args.version))
    elif args.action=='bind':ActivityStore(Path(args.agent_dir)/'activity.sqlite3').bind(args.provider,args.thread_id,args.session)


if __name__=='__main__':
    main()
