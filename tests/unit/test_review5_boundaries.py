"""Axiom review5 contract regressions; all workers are mocked.
Run with Python bytecode disabled and PYTHONPATH pointing to the target Axiom.
These tests use pytest tmp_path and never modify the installed engine.

R5-1 seal anchor_history survives re-seal (truncation alarm cannot be
    laundered by a later ordinary re-seal).
R5-2 replay cache is occurrence-scoped (a failure on occurrence k displaces
    only occurrence k's cached success, never earlier occurrences').
R5-3 a gated fan-out branch never spends a dispatch slot (and the worktree
    refusal path returns the reservation).
R5-4 an adoption declaration first made AT resume time is persisted back to
    run_context.json, so the next resume inherits it.
R5-5 adoption declarations are bound to the declaring spec version (a child
    spec resumed into the same run-dir does not silently inherit them).
"""
import contextlib, io, json, os
from pathlib import Path
from unittest.mock import patch
import pytest
from axiom.harness import Harness
from axiom.ir import Spec, Requirement, validate_spec, spec_to_json
from axiom.ledger import Ledger
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


def reseal_keeps_superseded_anchor():
    # Seal a ledger, truncate its tail event, then re-seal (the resume path
    # re-seals after re-walk). The superseded anchor is the ONLY remaining
    # proof of the truncated event; a re-seal must not launder it.
    ld = Ledger(BASE / 'reseal')
    ev = lambda i, tag: {'event_id': f'e{i}', 'kind': 'audit_marker',
                         'spec_version_id': 'recheck.v1',
                         'payload': {'tag': tag}, 'claims': []}
    ld.append(ev(1, 'first'))
    ld.append(ev(2, 'tail-that-will-vanish'))
    ld.seal()
    doomed_anchor = json.loads(ld.manifest_path.read_text())['final_event_hash']
    # Truncate the tail: the surviving chain is internally consistent.
    lines = ld.events_path.read_text().splitlines()
    ld.events_path.write_text('\n'.join(lines[:-1]) + '\n')
    detected_before = ld.verify_chain()
    # Ordinary re-seal (resume_after_resolution probe): pre-R5-1 this
    # discarded the old anchor and the next verify_chain passed silently.
    ld.seal()
    manifest = json.loads(ld.manifest_path.read_text())
    return {'anchor_was_of_doomed_event': doomed_anchor not in {
                e['event_hash'] for e in ld._lines()},
            'detected_before_reseal': detected_before,
            'anchor_history': manifest.get('anchor_history'),
            'detected_after_reseal': ld.verify_chain()}


def earlier_occurrence_success_survives_later_failure():
    # Control flow [n1, n1]: occurrence 0 succeeds, occurrence 1 fails
    # (denials). Resume must replay occurrence 0 (zero fresh calls for it)
    # and re-dispatch only occurrence 1.
    n = node(failure_policy={'max_retries': 0, 'on_exhausted': 'degrade'})
    s = spec({'n1': n}, steps=['n1', 'n1'])
    replies = iter([(0, envelope({'x': 'occ0 success'})),
                    (0, envelope({'x': 'occ1 denied'}, denials=['Write denied']))])
    h = hnew('occ-scoped', lambda *a, **k: next(replies))
    first = h.run(s)
    calls = []
    h2 = Harness(h.run_dir,
                 worker_runner=lambda *a, **k: (calls.append(1)
                                              or (0, envelope({'x': 'occ1 fresh'}))))
    h2.build_replay_cache(s)
    resumed = h2.run(s)
    replayed = [e for e in h2.ledger.events() if e['kind'] == 'replay_hit']
    return {'resumed_tail': resumed.get('validated_output'),
            'fresh_calls': len(calls),
            'replay_hits': len(replayed)}


def gated_parallel_branch_keeps_slot():
    # max_agents=1, parallel body risk:high -> the branch gates out WITHOUT
    # spending the only dispatch slot; after resolve, the re-dispatch must
    # be admitted (pre-R5-3 the spent slot refused it).
    calls = []
    body = node('body', risk='high')
    p = {'type': 'parallel', 'id': 'p', 'over': '{{items}}', 'body': body,
         'concurrency': 1}
    s = spec({'p': p}, max_agents=1)
    h = hnew('gated-parallel',
             lambda *a, **k: (calls.append(1) or (0, envelope({'x': 1}))))
    h.run_parallel(p, {'items': [1]}, s.spec_version_id, s)
    gated_count = h._agent_count
    gid = next(e['gate_id'] for e in h.ledger.events()
               if e['kind'] == 'gate_open')
    h.resolve_gate(gid, 'allow')
    h.run_parallel(p, {'items': [1]}, s.spec_version_id, s)
    return {'count_while_gated': gated_count,
            'worker_calls_after_resolve': len(calls),
            'cap_tripped': any(e['kind'] == 'agent_cap_exhausted'
                               for e in h.ledger.events())}


def worktree_refusal_returns_slot():
    # isolation=worktree on a non-git project_root: setup fails AFTER the
    # reservation; the slot must be returned so a later dispatch is admitted.
    body = node('body', isolation='worktree')
    p = {'type': 'parallel', 'id': 'p', 'over': '{{items}}', 'body': body,
         'concurrency': 1}
    s = spec({'p': p}, max_agents=1)
    calls = []
    h = hnew('worktree-refusal',
             lambda *a, **k: (calls.append(1) or (0, envelope({'x': 1}))))
    with patch('axiom.harness.Harness._worktree_setup', lambda self, *a, **k: None):
        h.run_parallel(p, {'items': [1]}, s.spec_version_id, s)
    count_after_refusal = h._agent_count
    body2 = node('body')
    p2 = {'type': 'parallel', 'id': 'p', 'over': '{{items}}', 'body': body2,
          'concurrency': 1}
    h.run_parallel(p2, {'items': [1]}, s.spec_version_id, s)
    return {'count_after_refusal': count_after_refusal,
            'worker_calls_after': len(calls),
            'cap_tripped': any(e['kind'] == 'agent_cap_exhausted'
                               for e in h.ledger.events())}


def resume_time_adoption_persists_to_context():
    # resume --adopted-from E must write E back to run_context.json so the
    # NEXT resume (no flag) still inherits the declaration.
    project = BASE / 'adopt-resume'; project.mkdir()
    s = spec({'n1': node(verdict=True)}, se=['S1:R1=node:n1'])
    sp = project / 'spec.json'; sp.write_text(spec_to_json(s))
    rd = project / 'run'
    runner = lambda *a, **k: (0, envelope({'verdict': 'VERIFIED'}))
    with patch('axiom.cli._active_global_path', lambda: project / 'global.json'), \
         patch('axiom.harness._default_runner', runner):
        first = quiet(['run', str(sp), '--run-dir', str(rd)])
        second = quiet(['resume', str(sp), '--run-dir', str(rd),
                        '--adopted-from', 'entry-X'])
        ctx_after_resume1 = json.loads((rd / 'run_context.json').read_text())
        third = quiet(['resume', str(sp), '--run-dir', str(rd)])
        ctx_after_resume2 = json.loads((rd / 'run_context.json').read_text())
    return {'run_exit': first[0], 'resume1_exit': second[0],
            'resume2_exit': third[0],
            'ctx1_adopted_from': ctx_after_resume1.get('adopted_from'),
            'ctx1_svid': ctx_after_resume1.get('adopted_from_svid'),
            'ctx2_adopted_from': ctx_after_resume2.get('adopted_from')}


def child_spec_does_not_inherit_parent_adoption():
    # Parent (recheck.v1) declares adoption E in the run-dir. A child
    # (recheck.v2) resumed into the same run-dir without re-declaring must
    # NOT inherit E; a fresh `run` of the child into that dir is rejected.
    project = BASE / 'adopt-child'; project.mkdir()
    parent = spec({'n1': node(verdict=True)}, se=['S1:R1=node:n1'])
    spp = project / 'parent.json'; spp.write_text(spec_to_json(parent))
    rd = project / 'run'
    runner = lambda *a, **k: (0, envelope({'verdict': 'VERIFIED'}))
    child = spec({'n1': node(verdict=True)}, se=['S1:R1=node:n1'],
                 spec_version_id='recheck.v2', parent_spec_id='recheck.v1',
                 revision=2)
    spc = project / 'child.json'; spc.write_text(spec_to_json(child))
    with patch('axiom.cli._active_global_path', lambda: project / 'global.json'), \
         patch('axiom.harness._default_runner', runner):
        quiet(['run', str(spp), '--run-dir', str(rd),
               '--adopted-from', 'entry-X'])
        ctx_parent = json.loads((rd / 'run_context.json').read_text())
        # Child `run` into the same dir while the PARENT's context is still
        # in place (no --fresh, no re-declare): INVALID. (A child resume
        # rewrites the context under its own svid, after which `run` is
        # legitimately admitted -- so the rejection must be probed first.)
        rejected = quiet(['run', str(spc), '--run-dir', str(rd)])
        # Child resumed without re-declaring: NOTE on stderr, no inheritance.
        resumed = quiet(['resume', str(spc), '--run-dir', str(rd)])
        ctx_child = json.loads((rd / 'run_context.json').read_text())
    return {'parent_ctx_adopted_from': ctx_parent.get('adopted_from'),
            'parent_ctx_svid': ctx_parent.get('adopted_from_svid'),
            'resume_exit': resumed[0], 'resume_stderr': resumed[2],
            'child_ctx_adopted_from': ctx_child.get('adopted_from'),
            'child_ctx_svid': ctx_child.get('adopted_from_svid'),
            'run_exit': rejected[0], 'run_stderr': rejected[2],
            'child_validation': validate_spec(child)}


def test_reseal_cannot_launder_truncation_alarm():
    r = reseal_keeps_superseded_anchor()
    assert r['anchor_was_of_doomed_event'], r
    assert r['detected_before_reseal'] and r['detected_after_reseal'], r
    assert any('truncated' in e or 'no event in the chain' in e
               for e in r['detected_after_reseal']), r


def test_resume_preserves_success_of_earlier_node_occurrence():
    r = earlier_occurrence_success_survives_later_failure()
    assert r['fresh_calls'] == 1, r
    assert r['replay_hits'] == 1, r
    assert r['resumed_tail'] == {'x': 'occ1 fresh'}, r


def test_gated_parallel_branch_does_not_spend_dispatch_slot():
    r = gated_parallel_branch_keeps_slot()
    assert r['count_while_gated'] == 0, r
    assert r['worker_calls_after_resolve'] == 1, r
    assert not r['cap_tripped'], r


def test_worktree_refusal_returns_reserved_slot():
    r = worktree_refusal_returns_slot()
    assert r['count_after_refusal'] == 0, r
    assert r['worker_calls_after'] == 1, r
    assert not r['cap_tripped'], r


def test_adoption_declared_during_resume_persists():
    r = resume_time_adoption_persists_to_context()
    assert r['run_exit'] == 0 and r['resume1_exit'] == 0, r
    assert r['ctx1_adopted_from'] == ['entry-X'], r
    assert r['ctx1_svid'] == 'recheck.v1', r
    assert r['ctx2_adopted_from'] == ['entry-X'], r


def test_child_spec_requires_explicit_adoption_declaration():
    r = child_spec_does_not_inherit_parent_adoption()
    assert r['parent_ctx_adopted_from'] == ['entry-X'], r
    assert r['resume_exit'] == 0 and 'not inherited' in r['resume_stderr'], r
    assert r['child_ctx_adopted_from'] == [], r
    assert r['child_ctx_svid'] == 'recheck.v2', r
    assert r['run_exit'] == 1 and 'INVALID' in r['run_stderr'], r
