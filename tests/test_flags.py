import json
import logging
from pathlib import Path

import pytest

from edscrape import flags
from edscrape.models import RoleClass

FIX = Path(__file__).parent / "fixtures" / "flags"
ROLE_MAP = {
    "admin": "staff",
    "staff": "staff",
    "tutor": "staff",
    "mentor": "helper",
    "student": "student",
}


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def run(name):
    data = load(name)
    users = {u["id"]: u for u in data["users"]}
    return flags.compute(data["thread"], users, ROLE_MAP, data["viewer_id"])


@pytest.fixture(autouse=True)
def _reset_warned():
    flags._warned.clear()


@pytest.fixture
def detail():
    return run("thread_detail")


def test_keys_parents_and_depths(detail):
    assert set(detail) == {
        "thread:1000", "answer:2001", "answer:2002", "comment:3001", "comment:3002",
        "comment:3003", "comment:3004", "comment:3005", "comment:3006", "comment:3007",
        "comment:3008", "comment:3009",
    }  # fmt: skip
    t = detail["thread:1000"]
    assert (t.kind, t.parent_key, t.depth) == ("thread", None, 0)
    a = detail["answer:2001"]
    assert (a.kind, a.parent_key, a.depth) == ("answer", "thread:1000", 1)
    assert detail["comment:3001"].parent_key == "answer:2001"
    assert detail["comment:3001"].depth == 2
    assert detail["comment:3002"].parent_key == "comment:3001"
    assert detail["comment:3002"].depth == 3
    top = detail["comment:3003"]
    assert (top.kind, top.parent_key, top.depth) == ("comment", "thread:1000", 1)


def test_depth_six_chain():
    posts = run("thread_deep")
    assert posts["thread:1001"].depth == 0
    assert posts["answer:5001"].depth == 1
    for cid in range(4002, 4008):
        p = posts[f"comment:{cid}"]
        assert p.depth == cid - 4000
        expected_parent = "answer:5001" if cid == 4002 else f"comment:{cid - 1}"
        assert p.parent_key == expected_parent
    assert len(posts) == 8


def test_roles_and_authors(detail):
    t = detail["thread:1000"]
    assert (t.author_name, t.course_role, t.role_class) == (
        "Student One", "student", RoleClass.student,
    )  # fmt: skip
    assert detail["answer:2001"].role_class is RoleClass.staff
    assert detail["comment:3002"].course_role == "tutor"
    assert detail["comment:3002"].role_class is RoleClass.staff
    left = detail["comment:3008"]
    assert left.course_role is None and left.role_class is RoleClass.unknown
    assert left.author_name == "Former Member"
    assert detail["comment:3009"].role_class is RoleClass.unknown


def test_content_and_passthrough(detail):
    a = detail["answer:2001"]
    assert a.content_xml.startswith('<document version="2.0">')
    assert a.text == "Staff answer."
    assert (a.number, a.editor_id, a.is_endorsed) == (1, 12, True)
    assert a.created_at == "2026-10-07T10:00:00.123456+11:00"
    assert a.updated_at == a.created_at
    assert detail["answer:2002"].editor_id is None
    assert detail["answer:2002"].is_endorsed is False


def test_accepted_only_on_matching_answer(detail):
    assert detail["answer:2001"].accepted is True
    assert detail["answer:2002"].accepted is False
    assert detail["thread:1000"].accepted is False
    assert all(not p.accepted for k, p in detail.items() if k != "answer:2001")


def test_accepted_id_collision_with_comment_id():
    thread = {"id": 1, "accepted_id": 7, "user_id": 1, "created_at": "2026-01-01T00:00:00Z",
              "answers": [{"id": 8, "user_id": 1, "type": "answer", "comments": []}],
              "comments": [{"id": 7, "user_id": 1, "type": "comment", "comments": []}]}  # fmt: skip
    posts = flags.compute(thread, {}, ROLE_MAP, 99)
    assert not posts["comment:7"].accepted
    assert not posts["answer:8"].accepted


def test_anonymous_preserves_is_own(detail):
    other = detail["comment:3004"]
    assert other.is_anonymous is True
    assert (other.user_id, other.author_name, other.course_role) == (None, "Anonymous", None)
    assert other.role_class is RoleClass.unknown
    assert other.is_own is False
    own = detail["comment:3005"]
    assert own.is_anonymous is True and own.is_own is True
    assert (own.user_id, own.author_name, own.course_role) == (None, "Anonymous", None)
    assert own.role_class is RoleClass.unknown


def test_own_non_anonymous(detail):
    own = detail["answer:2002"]
    assert own.is_own is True
    assert own.user_id == 14
    assert own.author_name == "Viewer Student"
    assert detail["answer:2001"].is_own is False


def test_tombstone_keeps_slot_and_children(detail):
    tomb = detail["comment:3006"]
    assert tomb.is_deleted is True
    assert (tomb.user_id, tomb.author_name, tomb.content_xml, tomb.text) == (None, "", "", "")
    assert (tomb.parent_key, tomb.depth) == ("thread:1000", 1)
    child = detail["comment:3007"]
    assert (child.parent_key, child.depth, child.is_deleted) == ("comment:3006", 2, False)
    assert tomb.later_staff_reply is False and tomb.accepted is False


def test_later_staff_reply_true_false_and_staff_self(detail):
    # student question at 09:00, staff answer at 10:00
    assert detail["thread:1000"].later_staff_reply is True
    # student comment 10:30, tutor reply 12:00
    assert detail["comment:3001"].later_staff_reply is True
    # own answer 11:00, tutor reply 12:00
    assert detail["answer:2002"].later_staff_reply is True
    # student comment after every staff post
    assert detail["comment:3003"].later_staff_reply is False
    # unknown-class posts count as non-staff
    assert detail["comment:3008"].later_staff_reply is True
    assert detail["comment:3004"].later_staff_reply is True  # anonymous: unknown class
    # staff are never flagged, even with a later staff post
    assert detail["answer:2001"].later_staff_reply is False
    assert detail["comment:3002"].later_staff_reply is False


def test_deleted_staff_post_does_not_count():
    thread = {
        "id": 1, "user_id": 11, "created_at": "2026-10-07T09:00:00+11:00",
        "answers": [{"id": 2, "type": "", "created_at": "2026-10-07T10:00:00+11:00",
                     "user_id": 12, "comments": []}],
    }  # fmt: skip
    users = {11: {"id": 11, "name": "S", "course_role": "student"},
             12: {"id": 12, "name": "T", "course_role": "admin"}}  # fmt: skip
    posts = flags.compute(thread, users, ROLE_MAP, 99)
    assert posts["thread:1"].later_staff_reply is False


def test_later_staff_reply_compares_datetimes_not_strings():
    # Staff 23:30+11:00 is 12:30Z; student 13:00Z is later in real time, although the
    # staff string sorts after the student string lexically.
    thread = {
        "id": 1, "user_id": 11, "created_at": "2026-10-07T13:00:00Z",
        "answers": [{"id": 2, "user_id": 12, "type": "answer", "comments": [],
                     "created_at": "2026-10-07T23:30:00.5+11:00"}],
    }  # fmt: skip
    users = {11: {"id": 11, "name": "S", "course_role": "student"},
             12: {"id": 12, "name": "T", "course_role": "staff"}}  # fmt: skip
    assert flags.compute(thread, users, ROLE_MAP, 99)["thread:1"].later_staff_reply is False
    thread["created_at"] = "2026-10-07T12:00:00Z"
    assert flags.compute(thread, users, ROLE_MAP, 99)["thread:1"].later_staff_reply is True


def test_list_item_without_answers_or_comments():
    posts = flags.compute(load("list_item"), {}, ROLE_MAP, 14)
    assert list(posts) == ["thread:1002"]
    p = posts["thread:1002"]
    assert (p.kind, p.parent_key, p.depth, p.number) == ("thread", None, 0, 102)
    assert p.author_name == "Student One"  # from the item's own `user` object
    assert p.role_class is RoleClass.student
    assert p.later_staff_reply is False and p.accepted is False


def test_thread_user_object_wins_over_users_map():
    thread = load("list_item")
    users = {11: {"id": 11, "name": "Other", "course_role": "admin"}}
    p = flags.compute(thread, users, ROLE_MAP, 99)["thread:1002"]
    assert p.author_name == "Student One"
    assert p.role_class is RoleClass.student


def test_unknown_role_warns_once(detail, caplog):
    flags._warned.clear()
    with caplog.at_level(logging.WARNING, logger="edscrape.flags"):
        run("thread_detail")
        run("thread_detail")
        assert flags.classify_role("teaching_fellow", ROLE_MAP) is RoleClass.unknown
    warnings = [r for r in caplog.records if "teaching_fellow" in r.getMessage()]
    assert len(warnings) == 1


def test_classify_role_mapping_and_null(caplog):
    with caplog.at_level(logging.WARNING, logger="edscrape.flags"):
        assert flags.classify_role("admin", ROLE_MAP) is RoleClass.staff
        assert flags.classify_role("tutor", ROLE_MAP) is RoleClass.staff
        assert flags.classify_role("mentor", ROLE_MAP) is RoleClass.helper
        assert flags.classify_role("student", ROLE_MAP) is RoleClass.student
        assert flags.classify_role(None, ROLE_MAP) is RoleClass.unknown
    assert caplog.records == []


def test_classify_role_bad_map_value_is_unknown():
    assert flags.classify_role("x", {"x": "wizard"}) is RoleClass.unknown
