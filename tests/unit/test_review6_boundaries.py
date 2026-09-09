"""Axiom review6 contract regressions; all workers are mocked.
Run with Python bytecode disabled and PYTHONPATH pointing to the target Axiom.
These tests use pytest tmp_path and never modify the installed engine.

R6-1 the replay cache is keyed by EXECUTION IDENTITY (run batch, control-flow
    occurrence position, attempt) instead of inferring occurrence from event
    counts: a retry is NOT a new occurrence, a repair-on-resume binds to the
    failed position, and a historical success is never replayed over a newer
    failure of the same position.
R6-2 a resume rejected by the run lock leaves run_context.json byte-identical
    (context read/merge/write moved inside the lock).
R6-3 adoption declarations are per-version state: a valid child spec that
    explicitly declares a different contract is accepted by `run` (same rule
    as `resume`), and a declaration made under one svid survives a rewrite by
    another svid's invocation (adopted_from_map).
"""
import contextlib, io, json, os, subprocess, sys
from pathlib import Path
from unittest.mock import patch
import pytest
from axiom.harness import Harness
from axiom.ir import Spec, Requirement, validate_spec, spec_to_json
from axiom.ledger import Ledger
from axiom.state import derive_verdict
from axiom.wiki import Wiki, extract_contract_entry
import axiom.cli as cli

BASE = None

@pytest.fixture(autouse=True)
def isolate_review(tmp_path, monkeypatch):
    global BASE
    BASE = tmp_path
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AXIOM_RUNTIME", "runner")
    monkeypatch.setattr(cli, "_active_global_path", lambda: tmp_path / "isolated-global.json")


def node(nid='n1', verdict=False, **kw):
    n = dict(type='agent', id=nid, prompt='audit mock {{item}}', output_schema={'type': 'object', 'required': ['verdict' if verdict else 'x']}, allowed_tools=[], write_areas=[], acceptance=['structured result'], failure_policy={'max_retries': 2, 'retry_guard': 'requires_new_evidence', 'on_exhausted': 'block'})
    if verdict:
        n['verdict_field'] = 'verdict'
    n.update(kw)
    return n

def spec(nodes, steps=None, se=None, requirements=None, **kw):
    s = Spec(spec_version_id='recheck.v1', parent_spec_id=None, revision=1, intent='engine audit fixture', requirements=requirements or [Requirement('R1', 'audit requirement')], boundaries=['mock execution only'], success_evidence=se or ['S1:R1=claim:C1'], nodes=nodes, control_flow={'type': 'sequence', 'steps': list(nodes) if steps is None else steps}, decision_trace=[], budget_usd=5., max_concurrent=2, max_agents=1000, max_stagnation=2)
    for k, v in kw.items():
        setattr(s, k, v)
    return s

def envelope(value, cost=0., denials=None, is_error=False):
    return json.dumps(dict(type='result', subtype='error' if is_error else 'success', is_error=is_error, num_turns=1, result=json.dumps(value), session_id='recheck-mock', total_cost_usd=cost, permission_denials=denials or []))

def hnew(name, runner=None, **kw):
    return Harness(BASE / name, worker_runner=runner, **kw)

def quiet(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = cli.main(argv)
    return rc, stdout.getvalue(), stderr.getvalue()


def capture(result):
    (BASE / 'observation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def seed_contract(project):
    wr = project / '.axiom/wiki'
    seed = spec({'n1': node(verdict=True)}, se=['S1:R1=node:n1'], intent='review5 reusable contract')
    h = Harness(project / 'seed-run', project_root=project,
                worker_runner=lambda *a, **k: (0, envelope({'verdict': 'VERIFIED'})))
    h.run(seed)
    w = Wiki(wr)
    entry = w.append(extract_contract_entry(seed, h.ledger, str(h.run_dir)))
    return w, wr, entry


def retry_is_not_a_new_occurrence():
    s = spec({'n1': node(failure_policy={'max_retries': 1, 'on_exhausted': 'degrade'})},
             steps=['n1', 'n1'])
    replies = iter([(1, ''), (0, envelope({'x': 'first done'})),
                    (0, envelope({'x': 'second done'}))])
    calls = []
    def worker(*a, **k):
        calls.append(1)
        return next(replies)
    h = hnew('retry-occurrence', worker)
    initial = h.run(s)
    initial_events = [e['kind'] for e in h.ledger.events()]
    resumed_calls = []
    h2 = Harness(h.run_dir, worker_runner=lambda *a, **k:
                 (resumed_calls.append(1) or (0, envelope({'x': 'unnecessary rerun'}))))
    h2.build_replay_cache(s)
    result = h2.run(s)
    return {'validation_errors': validate_spec(s), 'initial_calls': len(calls),
            'initial_events': initial_events, 'initial_result': initial,
            'resume_calls': len(resumed_calls), 'resumed_result': result}


def second_resume_reuses_first_resume_success():
    s = spec({'n1': node(failure_policy={'max_retries': 0, 'on_exhausted': 'degrade'})},
             steps=['n1', 'n1'])
    replies = iter([(0, envelope({'x': 'first done'})), (1, '')])
    h = hnew('resume-twice', lambda *a, **k: next(replies))
    h.run(s)
    counts = []
    for _ in range(2):
        calls = []
        resumed = Harness(h.run_dir, worker_runner=lambda *a, **k:
                          (calls.append(1) or (0, envelope({'x': 'repaired'}))))
        resumed.build_replay_cache(s)
        resumed.run(s)
        counts.append(len(calls))
    return {'validation_errors': validate_spec(s), 'first_resume_calls': counts[0],
            'second_resume_calls': counts[1]}


def latest_failure_clears_superseded_single_occurrence_successes():
    s = spec({'n1': node(failure_policy={'max_retries': 0, 'on_exhausted': 'degrade'})})
    replies = iter([(0, envelope({'x': 'old first'})), (0, envelope({'x': 'old second'})), (1, '')])
    h = hnew('single-node-three-runs', lambda *a, **k: next(replies))
    for _ in range(3):
        h.run(s)
    calls = []
    h2 = Harness(h.run_dir, worker_runner=lambda *a, **k:
                 (calls.append(1) or (0, envelope({'x': 'new verified attempt'}))))
    h2.build_replay_cache(s)
    resumed = h2.run(s)
    return {'validation_errors': validate_spec(s), 'fresh_calls': len(calls),
            'resumed': resumed}


def locked_resume_cannot_mutate_run_context():
    rd = BASE / 'locked-run'; rd.mkdir()
    s = spec({'n1': node()})
    sp = BASE / 'locked-spec.json'; sp.write_text(spec_to_json(s))
    context_path = rd / 'run_context.json'
    context_path.write_text(json.dumps({
        'project_root': str(BASE), 'auto_wiki': True, 'wiki_dir': str(BASE / 'wiki-a'),
        'adopted_from': ['original-entry'], 'adopted_from_svid': s.spec_version_id,
    }))
    before = json.loads(context_path.read_text())
    lock_code = ('import fcntl,sys;f=open(sys.argv[1],"a");'
                 'fcntl.flock(f,fcntl.LOCK_EX);print("held",flush=True);sys.stdin.readline()')
    holder = subprocess.Popen([sys.executable, '-c', lock_code, str(rd / 'lock')],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    calls = []
    try:
        assert holder.stdout.readline().strip() == 'held'
        with patch('axiom.harness._default_runner', lambda *a, **k:
                   (calls.append(1) or (0, envelope({'x': 1})))):
            attempt = quiet(['resume', str(sp), '--run-dir', str(rd),
                             '--wiki-dir', str(BASE / 'wiki-b'),
                             '--adopted-from', 'attempted-entry'])
        after = json.loads(context_path.read_text())
    finally:
        try:
            holder.communicate('\n', timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill(); holder.communicate()
    return {'exit_code': attempt[0], 'stderr': attempt[2], 'worker_calls': len(calls),
            'before': before, 'after': after, 'context_changed': before != after}


def child_run_can_explicitly_adopt_a_different_contract():
    project = BASE / 'new-contract'; project.mkdir()
    w, wr, a = seed_contract(project)
    seed_b = spec({'n1': node(verdict=True)}, se=['S1:R1=node:n1'],
                  intent='different reusable contract B')
    hb = Harness(project / 'seed-b', project_root=project,
                 worker_runner=lambda *args, **k: (0, envelope({'verdict': 'VERIFIED'})))
    hb.run(seed_b)
    b = w.append(extract_contract_entry(seed_b, hb.ledger, str(hb.run_dir)))
    s1 = spec({'n1': node(verdict=True)}, se=['S1:R1=node:n1'], adopted_from=[a['entry_id']])
    s2 = spec({'n1': node(verdict=True, prompt='explicitly use the different B contract')},
              se=['S1:R1=node:n1'], spec_version_id='recheck.v2',
              parent_spec_id='recheck.v1', revision=2, adopted_from=[b['entry_id']])
    sp1 = project / 'spec.v1.json'; sp1.write_text(spec_to_json(s1))
    sp2 = project / 'spec.v2.json'; sp2.write_text(spec_to_json(s2)); rd = project / 'trial'
    with patch('axiom.harness._default_runner', lambda *args, **k: (0, envelope({'verdict': 'VERIFIED'}))):
        first = quiet(['run', str(sp1), '--run-dir', str(rd),
                       '--auto-wiki', '--wiki-dir', str(wr)])
        second = quiet(['run', str(sp2), '--run-dir', str(rd),
                        '--auto-wiki', '--wiki-dir', str(wr), '--adopted-from', b['entry_id']])
        alternative = quiet(['resume', str(sp2), '--run-dir', str(rd)])
    return {'validation_errors': validate_spec(s2), 'initial_exit': first[0],
            'child_run_exit': second[0], 'child_run_stderr': second[2],
            'same_child_resume_exit': alternative[0],
            'parent_adopted': a['entry_id'], 'child_explicitly_adopted': b['entry_id']}


def test_worker_retry_does_not_shift_workflow_occurrence_cache():
    r = capture(retry_is_not_a_new_occurrence())
    assert r['resume_calls'] == 0, r


def test_second_resume_does_not_repeat_already_repaired_occurrence():
    r = capture(second_resume_reuses_first_resume_success())
    assert r['first_resume_calls'] == 1 and r['second_resume_calls'] == 0, r


def test_single_node_latest_failure_does_not_replay_superseded_success():
    r = capture(latest_failure_clears_superseded_single_occurrence_successes())
    assert r['fresh_calls'] == 1, r


def test_lock_rejection_leaves_run_context_unchanged():
    r = capture(locked_resume_cannot_mutate_run_context())
    assert not r['context_changed'] and r['worker_calls'] == 0, r


def test_valid_child_run_can_change_explicit_adopted_contract():
    r = capture(child_run_can_explicitly_adopt_a_different_contract())
    assert r['child_run_exit'] == 0, r
