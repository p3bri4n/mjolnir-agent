"""
Family F "verbatim" guarantee (docs/briefs/B3-benchmark-v2.md): T3/T5/T6/T10
must be the SAME wording/assertion as v1, checked here by identity against
test_web_tasks.py's TASKS rather than by eyeballing two copies of the same
text staying in sync (test_web_tasks_v2.py imports these tuples directly,
never re-declares them).
"""
import os

from tests_integration import test_web_tasks as v1
from tests_integration import test_web_tasks_v2 as v2


def test_family_f_task_ids_match_brief_order():
    assert v2.FAMILY_F_TASK_IDS == [
        "T3_tableau_dynamique",
        "T5_telechargement_calcul",
        "T6_session_authentifiee",
        "T10_books_toscrape",
    ]
    assert [t[0] for t in v2.FAMILY_F_TASKS] == v2.FAMILY_F_TASK_IDS


def test_family_f_tasks_are_the_same_objects_as_v1():
    v1_by_id = {t[0]: t for t in v1.TASKS}
    for task_id, prompt, assert_fn in v2.FAMILY_F_TASKS:
        v1_task_id, v1_prompt, v1_assert_fn = v1_by_id[task_id]
        assert prompt == v1_prompt
        assert assert_fn is v1_assert_fn  # same function object, not a re-implementation


def test_family_f_defaults_to_two_repetitions():
    # Only meaningful when the env override isn't set, same as the module
    # default it's asserting against (docs/briefs/B3-benchmark-v2.md: "2
    # repetitions instead of 3").
    if "WEB_TASKS_V2_REPETITIONS" not in os.environ:
        assert v2.N_REPETITIONS_V2 == 2
