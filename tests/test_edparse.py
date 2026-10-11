from pathlib import Path

import pytest

from edscrape.edparse import parse

FIX = Path(__file__).parent / "fixtures" / "content"

# fixture name -> (expected markdown, expected plain text)
CASES = {
    "paragraph": ("First para.\n\nSecond para.", "First para.\n\nSecond para."),
    "break": ("line one\nline two", "line one\nline two"),
    "bold": ("a **strong** word", "a strong word"),
    "italic": ("an *emphasised* word", "an emphasised word"),
    "underline": ("an underlined word", "an underlined word"),
    "strike": ("a ~~struck~~ word", "a struck word"),
    "code": (
        "No. Please use the provided project and create a separate folder `A2` under that project.",
        "No. Please use the provided project and create a separate folder A2 under that project.",
    ),
    "link": (
        "See [the spec](https://example.edu/spec) or [email us](mailto:unit@example.edu).",
        "See the spec or email us.",
    ),
    "list_bullet": (
        "- No, you only need to clone the provided repository.\n- Keep `SCENARIO.md`",
        "- No, you only need to clone the provided repository.\n- Keep SCENARIO.md",
    ),
    "list_number": ("1. First\n2. Second", "1. First\n2. Second"),
    "list_nested": (
        "- Parent\n  1. Child one\n  2. Child two\n- Sibling",
        "- Parent\n  1. Child one\n  2. Child two\n- Sibling",
    ),
    "heading": ("## Marking criteria\n\nBody", "Marking criteria\n\nBody"),
    "blockquote": (
        "> Quoted line one.\n>\n> Quoted line two.\n\nReply.",
        "Quoted line one.\n\nQuoted line two.\n\nReply.",
    ),
    "callout": (
        "> **info:** Extension requests close Friday.",
        "Extension requests close Friday.",
    ),
    "figure": ("![image](https://static.au.edusercontent.com/files/example.png)", "[image]"),
    "file": (
        "Attached.\n\n[schedule.py](https://static.au.edusercontent.com/files/x)",
        "Attached.\n\n[file: schedule.py]",
    ),
    "snippet": (
        "```py\n    def find_student_by_id(self, student_id):\n"
        "        return self.students.get(student_id)\n```\n\nwould this code work?",
        "    def find_student_by_id(self, student_id):\n"
        "        return self.students.get(student_id)\n\nwould this code work?",
    ),
    "web_snippet": (
        "```html\n<p>hi</p>\n```\n\n```css\np { color: red; }\n```",
        "<p>hi</p>\np { color: red; }",
    ),
    "mention": ("@Student thanks for asking.", "@Student thanks for asking."),
}


def load(name: str) -> str:
    return (FIX / f"{name}.xml").read_text()


@pytest.mark.parametrize("name", sorted(CASES))
def test_tag_fixture(name):
    md, text = CASES[name]
    result = parse(load(name), "FALLBACK")
    assert result.markdown == md
    assert result.text == text
    assert result.unknown_tags == []


def test_every_fixture_has_a_case():
    on_disk = {p.stem for p in FIX.glob("*.xml")}
    assert on_disk == set(CASES) | {"unknown", "malformed"}


def test_unknown_tags_keep_text_and_dedupe_in_order():
    result = parse(load("unknown"), "FALLBACK")
    assert result.markdown == "before x^2 after\n\nhidden\n\ny"
    assert result.text == "before x^2 after\n\nhidden\n\ny"
    assert result.unknown_tags == ["math", "spoiler"]


def test_malformed_xml_falls_back():
    result = parse(load("malformed"), "plain fallback")
    assert result.markdown == "plain fallback"
    assert result.text == "plain fallback"
    assert result.unknown_tags == ["<malformed>"]


def test_missing_document_root_falls_back():
    result = parse("<paragraph>x</paragraph>", "fb")
    assert (result.markdown, result.text, result.unknown_tags) == ("fb", "fb", ["<malformed>"])


def test_empty_content_gives_empty_strings():
    for empty in ("", "   \n"):
        result = parse(empty, "ignored")
        assert (result.markdown, result.text, result.unknown_tags) == ("", "", [])


def test_empty_document_gives_empty_strings():
    result = parse('<document version="2.0"></document>', "ignored")
    assert (result.markdown, result.text, result.unknown_tags) == ("", "", [])


def test_escaped_entities_decode():
    result = parse('<document version="2.0"><paragraph>a &lt; b &amp; c</paragraph></document>', "")
    assert result.markdown == "a < b & c"


def test_inline_code_containing_backtick():
    xml = '<document version="2.0"><paragraph><code>a`b</code></paragraph></document>'
    assert parse(xml, "").markdown == "``a`b``"


def test_mixed_inline_formatting_nests():
    xml = (
        '<document version="2.0"><paragraph><bold>b <italic>bi</italic></bold> '
        '<link href="https://x.test">l</link></paragraph></document>'
    )
    result = parse(xml, "")
    assert result.markdown == "**b *bi*** [l](https://x.test)"
    assert result.text == "b bi l"
