from axiom.harness import Harness


def test_build_worker_prompt_injects_acceptance():
    # #8: acceptance was a dead field (validate required it but no agent ever
    # saw it); _build_worker_prompt now injects it so the worker self-checks
    # before returning.
    node = {"output_schema": {"type": "object", "required": ["x"]},
            "acceptance": ["file modified", "x field present"]}
    p = Harness._build_worker_prompt(node, "do the task")
    assert "do the task" in p
    assert "ACCEPTANCE CRITERIA" in p
    assert "file modified" in p
    assert "x field present" in p


def test_build_worker_prompt_acceptance_without_schema():
    # acceptance injects even with no output_schema -- non-structured output
    # still benefits from self-check criteria.
    node = {"acceptance": ["service restarted"]}
    p = Harness._build_worker_prompt(node, "restart svc")
    assert "ACCEPTANCE CRITERIA" in p
    assert "service restarted" in p


def test_build_worker_prompt_no_acceptance_no_block():
    node = {"output_schema": {"type": "object"}}
    p = Harness._build_worker_prompt(node, "task")
    assert "ACCEPTANCE CRITERIA" not in p  # no acceptance declared -> no acc block
