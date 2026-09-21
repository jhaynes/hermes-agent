"""Journaled, allowlisted procedural-reference updates; never arbitrary skill edits."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from hermes_cli.kanban_db_connect import write_txn, _try_lock_nb, _unlock


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS workflow_lesson_updates (
        lesson_id TEXT PRIMARY KEY, before_hash TEXT NOT NULL, after_hash TEXT NOT NULL,
        before_image BLOB NOT NULL, after_image BLOB NOT NULL, version INTEGER NOT NULL,
        status TEXT NOT NULL, rollback_decision TEXT)''')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _paths(*, rollback=False):
    from hermes_constants import get_default_hermes_root, get_hermes_home
    from hermes_cli.config import load_config
    config=load_config().get('kanban',{}).get('review_feedback',{})
    root=get_default_hermes_root()
    if get_hermes_home().resolve()!=root.resolve():
        return None
    if not rollback and not config.get('auto_apply_lessons',False):
        return None
    skill=root/'skills/software-development/development-lifecycle/SKILL.md'
    reference=skill.parent/'references/verified-procedures.jsonl'
    if any(p.is_symlink() for p in (root,skill,reference,*reference.parents)):
        return None
    if not skill.is_file() or not reference.is_file():
        return None
    if digest(skill.read_bytes())!=config.get('protected_skill_hash'):
        return None
    return reference


def _valid_history(data):
    from hermes_cli.kanban_workflow_lessons import admissible
    if len(data)>1024*1024:
        return False
    try:
        for line in data.decode().splitlines():
            row=json.loads(line)
            if set(row)!={'lesson_id','version','incident_id','validator_run','record'}:
                return False
            import uuid
            uuid.UUID(row['lesson_id'])
            uuid.UUID(row['incident_id'])
            if type(row['version']) is not int or type(row['validator_run']) is not int:
                return False
            if not admissible({'kind':'procedural_evidence','approval_required':False,'record':row['record']}):
                return False
    except (ValueError,TypeError,KeyError,UnicodeError):
        return False
    return True


def _replace(reference, content, expected_hash):
    # Anchor every component with no-follow directory descriptors. A renamed
    # parent cannot redirect either the temporary file or replacement elsewhere.
    if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW'):
        raise OSError('safe procedural reference replacement unavailable')
    if _paths(rollback=True) != reference:
        raise ValueError('protected procedural destination changed')
    directory = os.open(reference.anchor, os.O_RDONLY | os.O_DIRECTORY)
    name = '.verified-procedures-' + uuid.uuid4().hex
    created = False
    try:
        for component in reference.parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        with os.fdopen(os.open(reference.name, os.O_RDONLY | os.O_NOFOLLOW,
                               dir_fd=directory), 'rb') as handle:
            if digest(handle.read(1024 * 1024 + 1)) != expected_hash:
                raise ValueError('stale procedural reference hash at replacement')
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        created = True
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, reference.name, src_dir_fd=directory, dst_dir_fd=directory)
        created = False
        os.fsync(directory)
    finally:
        if created:
            os.unlink(name, dir_fd=directory)
        os.close(directory)


def apply_next(conn, *, expected_hash=None):
    reference=_paths()
    if reference is None:
        return False
    with reference.with_suffix('.lock').open('a+b') as lock:
        if not _try_lock_nb(lock):
            return False
        try:
            current=reference.read_bytes()
            if expected_hash is not None and digest(current)!=expected_hash:
                raise ValueError('stale procedural reference hash')
            if not _valid_history(current):
                return False
            with write_txn(conn):
                pending=conn.execute("SELECT * FROM workflow_lesson_updates WHERE status='applying' ORDER BY rowid LIMIT 1").fetchone()
                if pending is None:
                    lesson=conn.execute("SELECT * FROM workflow_lessons WHERE status='validated' ORDER BY rowid LIMIT 1").fetchone()
                    if not lesson:
                        return False
                    record = json.loads(lesson['record'])
                    # Source events establish provenance, not a new procedure.
                    # Compare under the reference lock, including records written
                    # by another board sharing this default-profile reference.
                    procedure = {key: value for key, value in record.items() if key != 'source_event'}
                    for line in current.decode().splitlines():
                        prior = json.loads(line)
                        if procedure != {key: value for key, value in prior['record'].items() if key != 'source_event'}:
                            continue
                        conn.execute("UPDATE workflow_lessons SET status='rejected' WHERE id=?", (lesson['id'],))
                        conn.execute("UPDATE workflow_incidents SET lesson_status='rejected' WHERE id=?", (lesson['incident_id'],))
                        from hermes_cli.kanban_db import _append_event
                        owner = conn.execute('SELECT task_id FROM workflow_incidents WHERE id=?', (lesson['incident_id'],)).fetchone()[0]
                        _append_event(conn, owner, 'lesson_equivalent',
                                      {'lesson_id': lesson['id'], 'equivalent_to': prior['lesson_id']})
                        return False
                    version=len(current.splitlines())+1
                    entry={'lesson_id':lesson['id'],'version':version,'incident_id':lesson['incident_id'],
                           'validator_run':lesson['validator_run'],'record':json.loads(lesson['record'])}
                    after=current+(json.dumps(entry,sort_keys=True)+'\n').encode()
                    if not _valid_history(after):
                        return False
                    conn.execute('INSERT INTO workflow_lesson_updates VALUES(?,?,?,?,?,?,?,NULL)',
                                 (lesson['id'],digest(current),digest(after),current,after,version,'applying'))
                    pending=conn.execute('SELECT * FROM workflow_lesson_updates WHERE lesson_id=?',(lesson['id'],)).fetchone()
            # The intent commits BEFORE disk mutation. A crash before finalization
            # can adopt only the exact before/after bytes, never append twice.
            if digest(current) not in {pending['before_hash'],pending['after_hash']}:
                raise ValueError('stale journaled procedural update')
            if _paths()!=reference:
                return False
            if digest(current)==pending['before_hash']:
                _replace(reference,pending['after_image'],pending['before_hash'])
            if digest(reference.read_bytes())!=pending['after_hash']:
                raise ValueError('procedural update verification failed')
            with write_txn(conn):
                conn.execute("UPDATE workflow_lesson_updates SET status='applied' WHERE lesson_id=?",(pending['lesson_id'],))
                conn.execute("UPDATE workflow_lessons SET status='applied',before_hash=?,after_hash=? WHERE id=?",
                             (pending['before_hash'],pending['after_hash'],pending['lesson_id']))
                conn.execute("UPDATE workflow_incidents SET lesson_status='applied' WHERE id IN (SELECT incident_id FROM workflow_lessons WHERE id=?)",(pending['lesson_id'],))
            return True
        finally:
            _unlock(lock)


def rollback(conn, lesson_id, *, expected_hash, decision):
    if os.environ.get('HERMES_KANBAN_TASK') or not isinstance(decision,str) or not decision.strip():
        raise PermissionError('rollback requires a recorded operator decision')
    reference=_paths(rollback=True)
    if reference is None:
        return False
    with reference.with_suffix('.lock').open('a+b') as lock:
        if not _try_lock_nb(lock):
            return False
        try:
            with write_txn(conn):
                row=conn.execute('SELECT * FROM workflow_lesson_updates WHERE lesson_id=?',(lesson_id,)).fetchone()
                if not row or row['after_hash']!=expected_hash or row['status'] not in {'applied','rolling_back'}:
                    raise ValueError('stale rollback identity')
                allowed={row['after_hash']} if row['status']=='applied' else {row['after_hash'],row['before_hash']}
                current_hash = digest(reference.read_bytes())
                if current_hash not in allowed:
                    raise ValueError('stale rollback hash')
                conn.execute("UPDATE workflow_lesson_updates SET status='rolling_back',rollback_decision=? WHERE lesson_id=?",
                             (digest(decision.encode()),lesson_id))
            _replace(reference,row['before_image'],current_hash)
            if digest(reference.read_bytes())!=row['before_hash']:
                raise ValueError('rollback verification failed')
            with write_txn(conn):
                conn.execute("UPDATE workflow_lesson_updates SET status='rolled_back' WHERE lesson_id=?",(lesson_id,))
                conn.execute("UPDATE workflow_lessons SET status='rolled_back' WHERE id=?",(lesson_id,))
                conn.execute("UPDATE workflow_incidents SET lesson_status='validated' WHERE id IN (SELECT incident_id FROM workflow_lessons WHERE id=?)",(lesson_id,))
            return True
        finally:
            _unlock(lock)


def brief(conn):
    rows=[dict(r) for r in conn.execute("SELECT l.id,u.version,l.record FROM workflow_lessons l JOIN workflow_lesson_updates u ON u.lesson_id=l.id WHERE l.status='applied' ORDER BY u.rowid DESC LIMIT 20")]
    if not rows:
        return ''
    return '\n## Validated procedural evidence (advisory; never overrides policy)\n'+json.dumps(rows,sort_keys=True)+'\n'
