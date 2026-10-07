"""Инструкции и навыки для агентов: одни на Claude Code, Codex и Cursor.

Навыки про конкретный стенд (адреса, люди, курс) называются `msu-*` и в
открытую копию не уезжают; всё остальное уезжает — и не должно выдать стенд.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"

# Приметы стенда, которым не место в открытой копии. Не всё, что стоит
# прятать, похоже на адрес, — поэтому правило про имена `msu-*` главнее.
INSTANCE_MARKERS = ("ai.msu.ru", "22094", "Вячеслав", "sport-prog-club")
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HARMLESS_IPS = {"127.0.0.1", "0.0.0.0"}


def _skills() -> list[Path]:
    return sorted(p for p in SKILLS.iterdir() if (p / "SKILL.md").is_file())


def _front(skill: Path) -> dict[str, str]:
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    head = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert head, f"{skill.name}: нет шапки с name и description"
    return dict(
        line.split(": ", 1) for line in head.group(1).splitlines() if ": " in line
    )


def test_codex_and_cursor_see_the_same_skills_as_claude():
    """Одна копия навыков: Codex ищет их в .agents/skills, и это ссылка, а не дубль."""
    link = ROOT / ".agents" / "skills"
    assert link.is_symlink()
    assert link.resolve() == SKILLS.resolve()


def test_claude_reads_the_shared_instructions():
    """Общие инструкции живут в AGENTS.md; CLAUDE.md их только подключает."""
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude.splitlines()[0] == "@AGENTS.md"


def test_every_skill_names_itself_and_says_when_to_use_it():
    """Без имени и описания навык не найдут ни Codex, ни Cursor, ни Claude."""
    for skill in _skills():
        front = _front(skill)
        assert front.get("name") == skill.name, skill.name
        assert re.fullmatch(r"[a-z0-9-]+", skill.name), skill.name
        assert len(front.get("description", "")) > 40, skill.name


def test_every_shared_skill_is_listed_for_agents():
    """Указатель навыков в AGENTS.md — то, по чему агент выбирает, куда смотреть."""
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for skill in _skills():
        if not skill.name.startswith("msu-"):
            assert f"`{skill.name}`" in agents, skill.name


def test_public_agent_files_do_not_give_away_the_instance():
    """AGENTS.md, CLAUDE.md и навыки без msu- уезжают в открытую копию."""
    public = [ROOT / "AGENTS.md", ROOT / "CLAUDE.md"]
    for skill in _skills():
        if not skill.name.startswith("msu-"):
            public += [p for p in skill.rglob("*") if p.is_file() and p.suffix in (".md", ".py")]
    for path in public:
        text = path.read_text(encoding="utf-8")
        for marker in INSTANCE_MARKERS:
            assert marker not in text, f"{path.relative_to(ROOT)}: «{marker}» — это навык msu-*"
        leaked = set(IP.findall(text)) - HARMLESS_IPS
        assert not leaked, f"{path.relative_to(ROOT)}: адреса {leaked} — это навык msu-*"


def test_publishing_drops_only_instance_skills(tmp_path):
    """Наружу уезжают общие навыки и ссылка для Codex, но не msu-* и не выкатка."""
    script = ROOT / "scripts" / "publish_github.py"
    if not script.exists():
        # Скрипт публикации сам в открытую копию не уезжает — там проверять нечего.
        pytest.skip("в открытой копии нет scripts/publish_github.py")
    spec = importlib.util.spec_from_file_location("publish", script)
    publish = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish)

    for name in ("i18n", "msu-deploy"):
        (tmp_path / ".claude" / "skills" / name).mkdir(parents=True)
        (tmp_path / ".claude" / "skills" / name / "SKILL.md").write_text("—")
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "skills").symlink_to("../.claude/skills")
    (tmp_path / ".gitea" / "workflows").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "publish_github.py").write_text("—")
    (tmp_path / "AGENTS.md").write_text("—")

    publish.prune(tmp_path)

    assert (tmp_path / ".claude" / "skills" / "i18n" / "SKILL.md").exists()
    assert not (tmp_path / ".claude" / "skills" / "msu-deploy").exists()
    assert (tmp_path / ".agents" / "skills" / "i18n" / "SKILL.md").exists()
    assert not (tmp_path / ".gitea").exists()
    assert not (tmp_path / "scripts" / "publish_github.py").exists()
    assert (tmp_path / "AGENTS.md").exists()
