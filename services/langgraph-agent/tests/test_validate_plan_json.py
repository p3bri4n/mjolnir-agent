"""
_validate_plan_json (app/graph.py): pure JSON parsing/validation, no
docker/LLM. Shared by revise_plan — the planner node this used to also
cover (plan_task) was removed, docs/resolved-bugs.md #61.
"""

import json

import pytest


def test_validate_plan_json_accepts_well_formed_plan():
    import app.graph as g

    raw = json.dumps(
        {
            "sous_taches": [
                {"description": "Ouvrir le catalogue", "critere_succes": "page catalogue affichée"},
                {"description": "Trouver le prix", "critere_succes": "prix visible"},
            ]
        }
    )
    result = g._validate_plan_json(raw)
    assert result == [
        {"description": "Ouvrir le catalogue", "success_criterion": "page catalogue affichée", "tools": []},
        {"description": "Trouver le prix", "success_criterion": "prix visible", "tools": []},
    ]


def test_validate_plan_json_strips_think_block_and_code_fence():
    import app.graph as g

    raw = (
        "<think>je réfléchis</think>```json\n"
        '{"sous_taches": [{"description": "A", "critere_succes": "B"}]}\n```'
    )
    result = g._validate_plan_json(raw)
    assert result == [{"description": "A", "success_criterion": "B", "tools": []}]


def test_validate_plan_json_rejects_invalid_json():
    import app.graph as g

    with pytest.raises(g.PlanValidationError, match="invalid JSON"):
        g._validate_plan_json("pas du json")


def test_validate_plan_json_rejects_missing_key():
    import app.graph as g

    with pytest.raises(g.PlanValidationError, match="sous_taches"):
        g._validate_plan_json(json.dumps({"autre_chose": []}))


def test_validate_plan_json_rejects_too_many_subtasks():
    import app.graph as g

    subtasks = [{"description": f"étape {i}", "critere_succes": "ok"} for i in range(9)]
    with pytest.raises(g.PlanValidationError, match="out of bounds"):
        g._validate_plan_json(json.dumps({"sous_taches": subtasks}))


def test_validate_plan_json_rejects_empty_subtask_list():
    import app.graph as g

    with pytest.raises(g.PlanValidationError, match="out of bounds"):
        g._validate_plan_json(json.dumps({"sous_taches": []}))


def test_validate_plan_json_rejects_blank_description():
    import app.graph as g

    with pytest.raises(g.PlanValidationError, match="description"):
        g._validate_plan_json(json.dumps({"sous_taches": [{"description": "  ", "critere_succes": "B"}]}))


def test_validate_plan_json_rejects_missing_success_criterion():
    import app.graph as g

    with pytest.raises(g.PlanValidationError, match="critere_succes"):
        g._validate_plan_json(json.dumps({"sous_taches": [{"description": "A"}]}))
