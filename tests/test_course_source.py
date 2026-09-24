"""Материалы курса приезжают из репозитория преподавателей сами.

Проверяем распаковку, а не сеть: важно, что архив кладётся целиком и что
чужому архиву нельзя выбраться за пределы каталога.
"""

from __future__ import annotations

import io
import tarfile
from tarfile import OutsideDestinationError

import pytest

from app.services import course


def _archive(files: dict[str, str], top: str = "algo-1") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_unpack_drops_the_archive_top_level(tmp_path):
    """В архиве репозитория содержимое лежит в папке — недели должны подняться."""
    target = tmp_path / "course"
    files = course.unpack(_archive({"sem1/01/card.md": "title: Неделя 1\n"}), target)

    assert files == 1
    assert (target / "sem1" / "01" / "card.md").read_text() == "title: Неделя 1\n"


def test_unpack_replaces_previous_materials(tmp_path):
    """Обновление подменяет каталог целиком: удалённая неделя должна исчезнуть."""
    target = tmp_path / "course"
    course.unpack(_archive({"sem1/01/card.md": "старая", "sem1/02/card.md": "вторая"}), target)
    course.unpack(_archive({"sem1/01/card.md": "новая"}), target)

    assert (target / "sem1" / "01" / "card.md").read_text() == "новая"
    assert not (target / "sem1" / "02").exists()


def test_unpack_refuses_to_escape_the_directory(tmp_path):
    """Архив приезжает по сети — «..» в имени не должен вынести файл наружу."""
    target = tmp_path / "course"
    (tmp_path / "secret.txt").write_text("не трогать")

    with pytest.raises(OutsideDestinationError):
        course.unpack(_archive({"../../secret.txt": "подмена"}, top=""), target)

    assert (tmp_path / "secret.txt").read_text() == "не трогать"


def test_course_root_prefers_the_volume(tmp_path, monkeypatch):
    """Свежие материалы в томе главнее копии, лежащей рядом с кодом."""
    monkeypatch.setattr(course.settings, "data_dir", tmp_path)
    assert course.course_root() == course.REPO_COPY

    (tmp_path / "course" / "sem1").mkdir(parents=True)
    assert course.course_root() == tmp_path / "course"
