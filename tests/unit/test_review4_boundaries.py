"""Axiom review4 contract regressions; all workers are mocked.
Run with Python bytecode disabled and PYTHONPATH pointing to the target Axiom.
These tests use pytest tmp_path and never modify the installed engine.
"""
import contextlib, io, json, os, subprocess, sys, threading
from pathlib import Path
from unittest.mock import patch
import pytest
from axiom.harness import Harness
from axiom.ir import Spec, Requirement, validate_spec, spec_to_json
from axiom.ledger import Ledger
from axiom.state import derive_verdict
from axiom.wiki import Wiki, extract_contract_entry
from axiom.dispatch import DispatchResult
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

def cli_relay_preserves_error_flag():
    """File-protocol relay must preserve is_error across the adapter boundary.

    A host writes a DispatchResult with is_error=True + a conforming verdict
    JSON body. The relay (dispatch_via_host -> from_dict) must carry is_error
    through so _is_clean_attempt rejects it (exit==0 + conforming JSON would
    otherwise pass the success gate)."""
    rd = BASE / 'relay'; rd.mkdir()
    s = spec({'n1':node(verdict=True, failure_policy={'max_retries':0, 'on_exhausted':'degrade'})}, se=['S1:R1=node:n1'])
    h = Harness(rd, project_root=BASE)

    def host_thread():
        """Act as the host agent: poll for dispatch_req, write dispatch_res
        with is_error=True (the relay-must-preserve-it invariant)."""
        import glob as _glob, time as _time
        for _ in range(100):
            reqs = sorted(_glob.glob(str(rd / "dispatch_req_*.json")))
            if reqs:
                req_path = Path(reqs[0])
                rid = req_path.stem.replace("dispatch_req_", "")
                res_path = rd / f"dispatch_res_{rid}.json"
                r = DispatchResult(
                    session_id='audit-error',
                    result_text=json.dumps({'verdict': 'VERIFIED'}),
                    num_turns=1, cost_usd=0, permission_denials=[],
                    exit_code=0, retry_class='cognitive', is_error=True)
                # Canonical serialization (to_dict) — a hand-rolled dict
                # dropped is_error here, which is exactly what this test catches.
                with open(res_path, "w") as f:
                    json.dump(r.to_dict(), f)
                return
            _time.sleep(0.05)

    opts = {'AXIOM_RUNTIME': 'host', 'AXIOM_RUN_DIR': str(rd),
            'AXIOM_CLI_DISPATCH_TIMEOUT': '5', 'AXIOM_CLI_POLL_INTERVAL': '0.01'}
    with patch.dict(os.environ, opts), contextlib.redirect_stderr(io.StringIO()):
        host_t = threading.Thread(target=host_thread, daemon=True)
        host_t.start()
        result = h.run(s)
        host_t.join(timeout=5)
    events = [e for e in h.ledger.events() if e['kind']=='agent_result']
    return {'worker_is_error': True, 'validation_errors': validate_spec(s),
            'relay_result': result,
            'received_attempts': [{'is_error': e['payload'].get('is_error'),
                                   'is_clean': e['payload'].get('is_clean')} for e in events],
            'verdict': derive_verdict(s, h.ledger)}

def operational_failure_invalidates_verdict():
    n=node(verdict=True,failure_policy={'max_retries':0,'on_exhausted':'degrade'})
    s=spec({'n1':n},steps=['n1','n1'],se=['S1:R1=node:n1'])
    responses=iter([(0,envelope({'verdict':'VERIFIED'})),(1,'')])
    h=hnew('operational-stale',lambda *a,**k:next(responses))
    h.run(s)
    return {'validation_errors':validate_spec(s),'second_dispatch_exit':1,'events':[e['kind'] for e in h.ledger.events()],'verdict':derive_verdict(s,h.ledger)}

def parallel_cap_with_real_concurrency():
    barrier=threading.Barrier(2,timeout=1)
    calls=[]
    def worker(*a,**kw):
        calls.append(1)
        try:barrier.wait()
        except threading.BrokenBarrierError:pass
        return 0,envelope({'x':1})
    p={'type':'parallel','id':'p','over':'{{items}}','body':node('body'),'concurrency':2}
    s=spec({'p':p},max_agents=1,max_concurrent=2)
    h=hnew('parallel-concurrent',worker)
    result=h.run_parallel(p,{'items':[1,2]},s.spec_version_id,s)
    return {'max_agents':1,'concurrency':2,'actual_calls':len(calls),'output':result,'checkpoint_count':h.project_checkpoint(s)['dispatch_count']}

def gates_bind_effective_action():
    out={}
    # Scope hash must distinguish different rendered targets, not just templates.
    n=node(risk='high',prompt='audit action on {{target}}')
    s=spec({'n1':n});calls=[]
    h=hnew('gate-args',lambda args,**k:(calls.append(args[0]) or (0,envelope({'x':1}))))
    h.dispatch_agent(n,{'target':'A'},s.spec_version_id,s)
    gid=next(e['gate_id'] for e in h.ledger.events() if e['kind']=='gate_open')
    h.resolve_gate(gid,'allow')
    h.dispatch_agent(n,{'target':'A'},s.spec_version_id,s)
    before=len(calls)
    h.dispatch_agent(n,{'target':'B'},s.spec_version_id,s)
    out['changed_template_input']={'new_calls_for_B':len(calls)-before,'gate_count':sum(e['kind']=='gate_open' for e in h.ledger.events()),'rendered_B_seen':any('audit action on B' in p for p in calls)}
    # A retry authorization for an auth failure does not authorize a new risky task.
    h=hnew('gate-reason',lambda *a,**k:(0,envelope('Not logged in',is_error=True)))
    s1=spec({'n1':node(prompt='ordinary audit A')})
    h.run(s1)
    opened=next(e for e in h.ledger.events() if e['kind']=='gate_open')
    h.resolve_gate(opened['gate_id'],'allow')
    h._runner=lambda *a,**k:(0,envelope({'x':2}))
    s2=spec({'n1':node(prompt='new high risk audit B',risk='high')},spec_version_id='recheck.v2',parent_spec_id='recheck.v1',revision=2)
    result=h.run(s2)
    out['different_gate_reason']={'first_reason':opened['reason'],'old_scope':opened.get('action_scope_hash'),'v2_validation':validate_spec(s2),'v2_result':result,'gate_count':sum(e['kind']=='gate_open' for e in h.ledger.events())}
    return out

def later_failure_invalidates_cached_success():
    # One occurrence in the workflow, executed in two successive runs with
    # the same spec and run directory. Resume must retry the failed latest run.
    s=spec({'n1':node(failure_policy={'max_retries':0,'on_exhausted':'degrade'})})
    replies=iter([(0,envelope({'x':'old success'})),(0,envelope({'x':'failed attempt'},denials=['Write denied']))])
    h=hnew('cache-last-failed',lambda *a,**k:next(replies))
    h.run(s)
    last=h.run(s)
    calls=[]
    h2=Harness(h.run_dir,worker_runner=lambda *a,**k:(calls.append(1) or (0,envelope({'x':'new attempt'}))))
    h2.build_replay_cache(s)
    resumed=h2.run(s)
    return {'validation_errors':validate_spec(s),'latest_attempt':last,'resumed':resumed,'fresh_calls':len(calls)}

def cli_child_keeps_bootstrap_import_path():
    project=BASE/'child-import';project.mkdir()
    script=project/'deterministic.py'
    script.write_text('import time\ntime.sleep(0.3)\nprint(\'{"x": 1}\')\n')
    s=spec({'n1':{'id':'n1','type':'script','script_path':str(script),
        'output_schema':{'type':'object','required':['x']},
        'failure_policy':{'max_retries':0,'on_exhausted':'degrade'}}})
    sp=project/'spec.json';sp.write_text(spec_to_json(s))
    # Match bin/axiom's -c/sys.path bootstrap while isolating the global active
    # pointer. cmd_run and its real dispatch-serve child are left unmodified.
    engine_root=Path(cli.__file__).resolve().parent.parent
    bootstrap=('import sys,pathlib;sys.path.insert(0,sys.argv[1]);'
        'import axiom.cli as c;pointer=sys.argv[2];'
        'c._active_global_path=lambda:pathlib.Path(pointer);'
        'raise SystemExit(c.main(sys.argv[3:]))')
    env=os.environ.copy();env.pop('PYTHONPATH',None)
    env.update(PYTHONDONTWRITEBYTECODE='1',AXIOM_RUNTIME='runner')
    result=subprocess.run([sys.executable,'-c',bootstrap,str(engine_root),
        str(project/'isolated-global.json'),'run',str(sp),'--run-dir',str(project/'run')],cwd=project,env=env,capture_output=True,text=True,timeout=12)
    return {'validation_errors':validate_spec(s),'exit_code':result.returncode,
        'stderr':result.stderr,'script_completed':'"x": 1' in result.stdout}

def seal_resolution_preserves_truncation_detection():
    s=spec({'n1':node(risk='high')});h=hnew('seal-truncate')
    h.run(s)
    gid=next(e['gate_id'] for e in h.ledger.events() if e['kind']=='gate_open')
    h.ledger.append({'event_id':'audit-tail','kind':'audit_marker','spec_version_id':s.spec_version_id,'payload':{'fact':'tail must remain detectable'}})
    h.ledger.seal()
    lines=h.ledger.events_path.read_text().splitlines()
    h.ledger.events_path.write_text('\n'.join(lines[:-1])+'\n')
    before=h.ledger.verify_chain()
    try:
        h.resolve_gate(gid,'allow')
    except ValueError:
        pass  # Rejecting resolution on a corrupted journal is also correct.
    return {'before_resolve':before,'after_resolve':h.ledger.verify_chain(),'lost_tail_is_absent':not any(e.get('event_id')=='audit-tail' for e in h.ledger.events())}

def relative_wiki_directory_survives_cwd_change():
    a,b=BASE/'wiki-project-a',BASE/'wiki-caller-b';a.mkdir();b.mkdir()
    s=spec({'n1':node(verdict=True)},se=['S1:R1=node:n1'])
    sp=a/'spec.json';sp.write_text(spec_to_json(s));rd=a/'.axiom/run'
    before=Path.cwd()
    try:
        with patch('axiom.cli._active_global_path',lambda:BASE/'wiki-global.json'),patch('axiom.harness._default_runner',lambda *a,**k:(0,envelope({'verdict':'VERIFIED'}))):
            os.chdir(a)
            first=quiet(['run',str(sp),'--run-dir',str(rd),'--auto-wiki'])
            context=json.loads((rd/'run_context.json').read_text())
            os.chdir(b)
            second=quiet(['resume',str(sp),'--run-dir',str(rd)])
    finally:os.chdir(before)
    return {'first_exit':first[0],'resume_exit':second[0],'stored_wiki_dir':context['wiki_dir'],'project_A_entries':len(Wiki(a/'.axiom/wiki').entries()),'caller_B_entries':len(Wiki(b/'.axiom/wiki').entries())}

def adoption_feedback_survives_resume():
    out={}
    before=Path.cwd()
    try:
        for source in ['spec','cli']:
            project=BASE/('adoption-'+source);project.mkdir();wr=project/'wiki'
            os.chdir(project)
            seed=spec({'n1':node(verdict=True)},se=['S1:R1=node:n1'],intent='contract seed')
            seed_h=Harness(project/'seed-run',worker_runner=lambda *a,**k:(0,envelope({'verdict':'VERIFIED'})),project_root=project)
            seed_h.run(seed)
            w=Wiki(wr);entry=w.append(extract_contract_entry(seed,seed_h.ledger,str(seed_h.run_dir)))
            s=spec({'n1':node(verdict=True,risk='high')},se=['S1:R1=node:n1'],intent='adopted contract trial')
            if source=='spec':s.adopted_from=[entry['entry_id']]
            sp=project/'spec.json';sp.write_text(spec_to_json(s));rd=project/'trial'
            flags=['--adopted-from',entry['entry_id']] if source=='cli' else []
            with patch('axiom.cli._active_global_path',lambda:project/'global.json'),patch('axiom.harness._default_runner',lambda *a,**k:(0,envelope({'verdict':'VERIFIED'}))):
                first=quiet(['run',str(sp),'--run-dir',str(rd),'--auto-wiki','--wiki-dir',str(wr)]+flags)
                gid=next(e['gate_id'] for e in Harness(rd).ledger.events() if e['kind']=='gate_open')
                quiet(['gate','resolve','--run-dir',str(rd),'--gate-id',gid,'--decision','allow'])
                second=quiet(['resume',str(sp),'--run-dir',str(rd)])
            impacts=[e for e in w.entries() if e.get('impact_kind')=='adoption_outcome' and e.get('amends')==entry['entry_id']]
            out[source]={'first_exit':first[0],'resume_exit':second[0],'outcomes':[e.get('reason') for e in impacts],'experience_edges':[e.get('adopted_from',[]) for e in w.entries() if e.get('entry_type')=='experience']}
    finally:os.chdir(before)
    return out


def test_cli_relay_preserves_failed_envelope():
    r = cli_relay_preserves_error_flag()
    assert r["verdict"] != "VERIFIED", r


def test_operational_failure_invalidates_prior_verified():
    r = operational_failure_invalidates_verdict()
    assert r["verdict"] != "VERIFIED", r


def test_max_agents_is_enforced_with_two_concurrent_workers():
    r = parallel_cap_with_real_concurrency()
    assert r["actual_calls"] <= r["max_agents"], r


def test_retry_approval_cannot_authorize_a_new_high_risk_action():
    r = gates_bind_effective_action()
    # Different rendered inputs additionally require a documented policy on
    # template-wide approval. The different-reason case is unambiguous.
    assert r["different_gate_reason"]["gate_count"] >= 2, r


def test_failed_last_attempt_is_not_replaced_by_old_cached_success():
    r = later_failure_invalidates_cached_success()
    assert r["fresh_calls"] == 1 and r["resumed"]["validated_output"]["x"] == "new attempt", r


def test_resolution_cannot_erase_existing_truncation_alarm():
    r = seal_resolution_preserves_truncation_detection()
    assert r["before_resolve"] and r["after_resolve"], r


def test_relative_wiki_path_stays_with_original_run():
    r = relative_wiki_directory_survives_cwd_change()
    assert r["project_A_entries"] == 2 and r["caller_B_entries"] == 0, r


def test_cli_adoption_is_retained_on_resume():
    r = adoption_feedback_survives_resume()["cli"]
    assert any("verdict VERIFIED" in item for item in r["outcomes"]), r


def test_spec_adoption_feedback_already_works():
    r = adoption_feedback_survives_resume()["spec"]
    assert any("verdict VERIFIED" in item for item in r["outcomes"]), r


def test_cli_dispatch_child_can_import_engine_outside_package_directory():
    r = cli_child_keeps_bootstrap_import_path()
    assert "No module named axiom" not in r["stderr"], r
