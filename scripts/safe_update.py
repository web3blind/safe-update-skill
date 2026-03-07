#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen
from xml.etree import ElementTree as ET

ROOT = Path('/home/assistent/.openclaw/workspace')
STATE_DIR = ROOT / '.safe-update'
BACKUP_DIR = STATE_DIR / 'backups'
STATE_FILE = STATE_DIR / 'state.json'
REPORT_FILE = STATE_DIR / 'last-report.md'
CFG_PATH = Path('/home/assistent/.openclaw/openclaw.json')

KEEP_LAST_SUCCESS = int(os.getenv('SAFE_UPDATE_KEEP_LAST_SUCCESS', '3'))
MAX_AGE_DAYS = int(os.getenv('SAFE_UPDATE_MAX_AGE_DAYS', '14'))
UPDATE_CMD = os.getenv('SAFE_UPDATE_UPDATE_CMD', '').strip()  # optional
MIN_FREE_SPACE_MB = int(os.getenv('SAFE_UPDATE_MIN_FREE_MB', '500'))  # minimum free space before backup
ENABLE_AUTO_ROLLBACK = os.getenv('SAFE_UPDATE_AUTO_ROLLBACK', '1') == '1'


def run(cmd: str):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def load_state():
    ensure_dirs()
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {
        'createdAt': now_iso(),
        'updatedAt': now_iso(),
        'step': 'init',
        'stepsDone': [],
        'backupPath': '',
        'backupSnapshotDir': '',
        'updateExecuted': False,
        'updateSkipped': False,
        'notes': [],
    }


def save_state(st):
    st['updatedAt'] = now_iso()
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')


def add_note(st, text):
    st['notes'].append(f"[{now_iso()}] {text}")


def check_disk_space(path: Path, required_mb: int = MIN_FREE_SPACE_MB) -> dict:
    """Check available disk space. Returns dict with 'ok' and 'available_mb'."""
    try:
        stat = shutil.disk_usage(path)
        available_mb = stat.free / (1024 * 1024)
        return {'ok': available_mb >= required_mb, 'available_mb': int(available_mb), 'required_mb': required_mb}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'available_mb': 0, 'required_mb': required_mb}


def precheck(st):
    checks = {}
    for name, cmd in [
        ('openclaw_status', 'openclaw status'),
        ('gateway_status', 'openclaw gateway status'),
    ]:
        c, o, e = run(cmd)
        checks[name] = {'ok': c == 0, 'out': (o or e)[:2000]}

    # check disk space before backup
    disk_check = check_disk_space(BACKUP_DIR)
    checks['disk_space'] = {'ok': disk_check['ok'], 'out': f"Available: {disk_check.get('available_mb', 0)}MB, required: {disk_check.get('required_mb', MIN_FREE_SPACE_MB)}MB"}
    if not disk_check['ok']:
        add_note(st, f"WARNING: Low disk space ({disk_check.get('available_mb', 0)}MB)")

    # optional config validate
    c, o, e = run('openclaw config validate --json')
    out = (o or e)
    if c != 0 and ('unknown' in out.lower() or 'not found' in out.lower() or 'invalid choice' in out.lower()):
        checks['config_validate'] = {'ok': True, 'out': 'skipped: command not available in this OpenClaw build'}
    else:
        checks['config_validate'] = {'ok': c == 0, 'out': out[:2000]}

    st['precheck'] = checks
    st['stepsDone'].append('precheck')
    add_note(st, 'Precheck completed')


def backup(st):
    ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    bdir = BACKUP_DIR / ts
    bdir.mkdir(parents=True, exist_ok=True)

    if CFG_PATH.exists():
        shutil.copy2(CFG_PATH, bdir / 'openclaw.json')

    # key context files
    key_files = [
        ROOT / 'MEMORY.md', ROOT / 'AGENTS.md', ROOT / 'SOUL.md', ROOT / 'USER.md',
        ROOT / 'chats.md', ROOT / 'CHATS.md', ROOT / 'memory' / 'telegram-topics.json'
    ]
    for p in key_files:
        if p.exists():
            rel = p.relative_to(ROOT) if str(p).startswith(str(ROOT)) else Path(p.name)
            out = bdir / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)

    # snapshot skill configs/state
    state_digest = Path('/home/assistent/.openclaw/state/digest/configs')
    if state_digest.exists():
        out = bdir / 'state' / 'digest' / 'configs'
        out.mkdir(parents=True, exist_ok=True)
        for f in state_digest.glob('*.json'):
            shutil.copy2(f, out / f.name)

    # compressed archive
    tar_path = BACKUP_DIR / f'{ts}.tar.gz'
    with tarfile.open(tar_path, 'w:gz') as tar:
        tar.add(bdir, arcname=ts)

    st['backupPath'] = str(tar_path)
    st['backupSnapshotDir'] = str(bdir)
    st['stepsDone'].append('backup')
    add_note(st, f'Backup completed: {tar_path}')


def fetch_release_summary(limit=2):
    url = 'https://github.com/openclaw/openclaw/releases.atom'
    with urlopen(url, timeout=20) as r:
        data = r.read()
    root = ET.fromstring(data)
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    items = []
    for e in root.findall('a:entry', ns)[:limit]:
        title = (e.findtext('a:title', default='', namespaces=ns) or '').strip()
        link_el = e.find('a:link', ns)
        link = link_el.attrib.get('href', '') if link_el is not None else ''
        content = (e.findtext('a:content', default='', namespaces=ns) or '')
        # strip HTML tags crudely
        txt = re.sub('<[^>]+>', ' ', content)
        txt = re.sub(r'\s+', ' ', txt).strip()
        items.append({'title': title, 'link': link, 'summary': txt[:1200]})
    return items


def analyze_release_impact(st):
    items = fetch_release_summary(limit=3)
    risk_keywords = ['telegram', 'topic', 'session', 'cron', 'heartbeat', 'stream', 'config', 'plugin', 'routing']
    impacts = []
    for it in items:
        text = (it['title'] + ' ' + it['summary']).lower()
        hits = [k for k in risk_keywords if k in text]
        impacts.append({'release': it['title'], 'link': it['link'], 'riskHits': hits})
    st['releaseImpact'] = impacts
    st['stepsDone'].append('release_impact')
    add_note(st, 'Release impact analysis completed')


def execute_update_step(st):
    if not UPDATE_CMD:
        st['updateSkipped'] = True
        add_note(st, 'Update step skipped (SAFE_UPDATE_UPDATE_CMD is empty).')
        return
    c, o, e = run(UPDATE_CMD)
    st['updateExecuted'] = (c == 0)
    st['updateOutput'] = (o or e)[:3000]
    add_note(st, f"Update command executed, ok={st['updateExecuted']}")


def postcheck(st):
    checks = {}
    for name, cmd in [
        ('openclaw_status_after', 'openclaw status'),
        ('gateway_status_after', 'openclaw gateway status'),
    ]:
        c, o, e = run(cmd)
        checks[name] = {'ok': c == 0, 'out': (o or e)[:2000]}

    # lightweight cron smoke
    c, o, e = run('python3 - <<\'PY\'\nimport json, pathlib\np=pathlib.Path("/home/assistent/.openclaw/workspace/memory/heartbeat-state.json")\nprint("ok" if p.exists() else "missing")\nPY')
    checks['heartbeat_state_present'] = {'ok': c == 0 and 'ok' in (o or ''), 'out': (o or e)[:2000]}

    st['postcheck'] = checks
    st['stepsDone'].append('postcheck')
    add_note(st, 'Postcheck completed')
    return checks


def rollback_from_backup(backup_path: str) -> bool:
    """Restore from backup archive. Returns True if successful."""
    if not backup_path or not Path(backup_path).exists():
        return False
    try:
        # Extract to temp location first
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with tarfile.open(backup_path, 'r:gz') as tar:
                tar.extractall(tmpdir)
            # Find the extracted folder
            extracted = list(Path(tmpdir).iterdir())
            if not extracted:
                return False
            # Restore key files
            root_dir = extracted[0]
            for f in ['MEMORY.md', 'AGENTS.md', 'SOUL.md', 'USER.md']:
                src = root_dir / f
                if src.exists():
                    shutil.copy2(src, ROOT / f)
            # Restore memory files
            src_memory = root_dir / 'memory'
            if src_memory.exists():
                dst_memory = ROOT / 'memory'
                dst_memory.mkdir(parents=True, exist_ok=True)
                for f in src_memory.glob('*'):
                    if f.is_file():
                        shutil.copy2(f, dst_memory / f.name)
            # Restore config
            src_cfg = root_dir / 'openclaw.json'
            if src_cfg.exists():
                shutil.copy2(src_cfg, CFG_PATH)
        return True
    except Exception as e:
        print(f"Rollback failed: {e}")
        return False


def cleanup_backups(st):
    ensure_dirs()
    archives = sorted(BACKUP_DIR.glob('*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True)

    # keep last N
    for idx, p in enumerate(archives):
        if idx >= KEEP_LAST_SUCCESS:
            try:
                p.unlink()
            except Exception:
                pass

    # age-based
    cutoff = time.time() - (MAX_AGE_DAYS * 86400)
    for p in BACKUP_DIR.glob('*.tar.gz'):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except Exception:
            pass

    st['stepsDone'].append('cleanup')
    add_note(st, f'Cleanup completed (keep={KEEP_LAST_SUCCESS}, age={MAX_AGE_DAYS}d)')


def render_report(st):
    lines = []
    lines.append('# Safe Update Report')
    lines.append(f"- Updated: {st.get('updatedAt')}")
    lines.append(f"- Backup: {st.get('backupPath') or 'none'}")
    lines.append(f"- Update executed: {st.get('updateExecuted', False)}")
    lines.append(f"- Update skipped: {st.get('updateSkipped', False)}")
    lines.append('')

    if st.get('releaseImpact'):
        lines.append('## Release impact')
        for r in st['releaseImpact']:
            lines.append(f"- {r['release']}: {', '.join(r['riskHits']) if r['riskHits'] else 'no major hits'}")
            if r.get('link'):
                lines.append(f"  - {r['link']}")
        lines.append('')

    if st.get('precheck'):
        lines.append('## Precheck')
        for k, v in st['precheck'].items():
            lines.append(f"- {k}: {'ok' if v.get('ok') else 'fail'}")
        lines.append('')

    if st.get('postcheck'):
        lines.append('## Postcheck')
        for k, v in st['postcheck'].items():
            lines.append(f"- {k}: {'ok' if v.get('ok') else 'fail'}")
        lines.append('')

    if st.get('rollbackPerformed'):
        lines.append('## Rollback')
        lines.append('- Status: performed (postcheck failed)')
        lines.append('')
    elif st.get('rollbackFailed'):
        lines.append('## Rollback')
        lines.append('- Status: FAILED')
        lines.append('')

    lines.append('## Notes')
    for n in st.get('notes', []):
        lines.append(f"- {n}")

    txt = '\n'.join(lines)
    REPORT_FILE.write_text(txt, encoding='utf-8')
    print(txt)


def do_dry_run(st):
    if 'precheck' not in st['stepsDone']:
        precheck(st)
    # Check disk space before proceeding
    disk_check = check_disk_space(BACKUP_DIR)
    if not disk_check['ok']:
        add_note(st, f"WARNING: Not enough disk space ({disk_check.get('available_mb', 0)}MB)")
    if 'backup' not in st['stepsDone']:
        backup(st)
    if 'release_impact' not in st['stepsDone']:
        analyze_release_impact(st)
    cleanup_backups(st)


def do_run(st):
    if 'precheck' not in st['stepsDone']:
        precheck(st)
    # Check disk space before proceeding
    disk_check = check_disk_space(BACKUP_DIR)
    if not disk_check['ok']:
        add_note(st, f"ERROR: Not enough disk space. Aborting update.")
        st['stepsDone'].append('abort')
        return
    if 'backup' not in st['stepsDone']:
        backup(st)
    if 'release_impact' not in st['stepsDone']:
        analyze_release_impact(st)
    if 'update' not in st['stepsDone']:
        execute_update_step(st)
        st['stepsDone'].append('update')
    if 'postcheck' not in st['stepsDone']:
        postcheck_results = postcheck(st)
        # Auto-rollback if postcheck failed
        if ENABLE_AUTO_ROLLBACK and st.get('updateExecuted'):
            postcheck_ok = all(v.get('ok', False) for v in postcheck_results.values())
            if not postcheck_ok:
                add_note(st, "WARNING: Postcheck failed, initiating rollback...")
                backup_path = st.get('backupPath', '')
                if rollback_from_backup(backup_path):
                    add_note(st, "Rollback completed successfully")
                    st['rollbackPerformed'] = True
                else:
                    add_note(st, "ERROR: Rollback failed!")
                    st['rollbackFailed'] = True
    if 'cleanup' not in st['stepsDone']:
        cleanup_backups(st)


def main():
    ensure_dirs()
    st = load_state()
    cmd = (os.sys.argv[1] if len(os.sys.argv) > 1 else 'dry-run').strip().lower()

    if cmd == 'dry-run':
        do_dry_run(st)
    elif cmd == 'run':
        do_run(st)
    elif cmd == 'resume':
        do_run(st)
    elif cmd == 'cleanup':
        cleanup_backups(st)
    else:
        print('Usage: safe_update.py [dry-run|run|resume|cleanup]')
        raise SystemExit(2)

    save_state(st)
    render_report(st)


if __name__ == '__main__':
    main()
