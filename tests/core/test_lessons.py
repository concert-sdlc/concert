"""Reading the lessons ledger: parsing, selection and the cap."""

from concert.core.lessons import Lesson, load_lessons, parse_lesson, select_lessons, stack_tags


def lesson_file(identifier: str = "L0001", **front: object) -> str:
    fields: dict[str, object] = {
        "id": identifier,
        "type": "pitfall",
        "applies_to": ["implementing"],
        "confidence": 0.8,
        "scope": "project",
        "status": "accepted",
        "created": "2026-10-01",
        "last_validated": "2026-10-20",
    } | front
    lines: list[str] = []
    for key, value in fields.items():
        lines.append(f"{key}: {value!r}" if isinstance(value, list | str) else f"{key}: {value}")
    return (
        "---\n"
        + "\n".join(lines)
        + "\n---\nPer-push CI exhausts minutes.\nRun checks locally first.\n"
    )


def ok(identifier: str = "L0001", **front: object) -> Lesson:
    parsed = parse_lesson(f"docs/lessons/{identifier}-x.md", lesson_file(identifier, **front))
    assert isinstance(parsed, Lesson), parsed
    return parsed


def test_a_well_formed_file_becomes_a_lesson() -> None:
    lesson = ok()
    assert (lesson.id, lesson.type, lesson.status, lesson.confidence) == (
        "L0001",
        "pitfall",
        "accepted",
        0.8,
    )
    assert lesson.applies_to == ("implementing",) and lesson.body.startswith("Per-push CI")


def test_malformed_files_say_what_is_wrong() -> None:
    cases = {
        "no frontmatter at all": "no frontmatter",
        lesson_file(id="bad"): "id must look like L0001",
        lesson_file(status="draft"): "status must be one of",
        lesson_file(confidence=2): "between 0 and 1",
        lesson_file(applies_to="implementing"): "applies_to must be a list",
        "---\nid: L0001\n": "unterminated",
    }
    for text, reason in cases.items():
        result = parse_lesson("docs/lessons/L0001-x.md", text)
        assert isinstance(result, str) and reason in result, (text, result)
    wrong_name = parse_lesson("docs/lessons/other.md", lesson_file())
    assert isinstance(wrong_name, str) and "file name must start with the id" in wrong_name


def test_load_reads_the_lessons_directory_and_reports_the_bad_ones() -> None:
    tree = {
        "docs/lessons/L0001-a.md": lesson_file("L0001"),
        "docs/lessons/L0002-b.md": "garbage",
        "docs/other.md": lesson_file("L0003"),
        "README.md": "x",
    }
    lessons, problems = load_lessons(tree)
    assert [x.id for x in lessons] == ["L0001"] and len(problems) == 1


def test_only_accepted_lessons_that_apply_are_selected() -> None:
    lessons = [
        ok("L0001"),
        ok("L0002", status="proposed"),
        ok("L0003", applies_to=["in_review"]),
        ok("L0004", applies_to=["**/*.py"]),
        ok("L0005", applies_to=["python"]),
        ok("L0006", applies_to=["rust"]),
        ok("L0007", applies_to=[]),
    ]
    chosen = select_lessons(lessons, "implementing", ["app/main.py", "README.md"])
    assert [x.id for x in chosen] == ["L0001", "L0004", "L0005", "L0007"]


def test_best_confidence_then_newest_validation_then_id_and_the_cap_applies() -> None:
    lessons = [
        ok("L0001", confidence=0.5, last_validated="2026-10-01"),
        ok("L0002", confidence=0.9, last_validated="2026-01-01"),
        ok("L0003", confidence=0.9, last_validated="2026-09-01"),
        ok("L0004", confidence=0.9, last_validated="2026-09-01"),
    ]
    assert [x.id for x in select_lessons(lessons, "implementing", [])] == [
        "L0003",
        "L0004",
        "L0002",
        "L0001",
    ]
    assert [x.id for x in select_lessons(lessons, "implementing", [], cap=2)] == ["L0003", "L0004"]


def test_stack_tags_come_from_file_names() -> None:
    assert stack_tags(["a/b.py", "web/app.tsx", "Dockerfile", "notes.txt"]) == {
        "python",
        "typescript",
        "docker",
    }
