import subprocess
from pathlib import Path


def test_repository_contains_no_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.check_output(
        ["git", "ls-files"], cwd=root, text=True, encoding="utf-8"
    ).splitlines()
    assert "case-set.json" not in tracked
    assert ".env" not in tracked
    assert not any(
        path.startswith(("inputs/", "outputs/", "traces/", "dist/"))
        and not path.endswith(".gitkeep")
        for path in tracked
    )
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(Path(path).name in forbidden for path in tracked)


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
