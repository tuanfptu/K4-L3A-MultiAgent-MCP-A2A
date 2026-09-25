import subprocess
from pathlib import Path


def test_repository_contains_no_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout.decode("utf-8").split("\0")
    paths = {Path(name).as_posix() for name in tracked if name}
    assert "case-set.json" not in paths
    assert not any(name.startswith("inputs/") and name.endswith(".json") for name in paths)
    assert not any(name.startswith("outputs/") and name.endswith(".json") for name in paths)
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(Path(name).name in forbidden for name in paths)


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
