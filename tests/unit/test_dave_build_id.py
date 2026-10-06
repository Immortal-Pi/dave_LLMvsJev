"""The deadly-dave build id (adapters/dave.py build_id): the game's sources and levels, not the bridge."""

from dave_agent.adapters.dave import BRIDGE_EXE, build_id


def game(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "res" / "levels").mkdir(parents=True)
    (tmp_path / "dave.c").write_text("int gravity = 1;\n")
    (tmp_path / "bridge.c").write_text("/* exports */\n")
    (tmp_path / "include" / "dave.h").write_text("#define W 16\n")
    (tmp_path / "res" / "levels" / "level1.ddt").write_bytes(b"\x01\x02")
    (tmp_path / BRIDGE_EXE).write_bytes(b"exe v1")
    return tmp_path


def test_the_bridge_does_not_change_the_build_id(tmp_path):
    d = game(tmp_path)
    first = build_id(d)
    (d / "bridge.c").write_text("/* exports monster routes too */\n")
    (d / BRIDGE_EXE).write_bytes(b"exe v2")
    assert build_id(d) == first and first.startswith("deadly-dave-")
    (d / "dave.c").write_bytes(b"int gravity = 1;\r\n")  # line endings from a Windows checkout
    assert build_id(d) == first


def test_physics_or_levels_change_the_build_id(tmp_path):
    d = game(tmp_path)
    first = build_id(d)
    (d / "dave.c").write_text("int gravity = 2;\n")
    second = build_id(d)
    (d / "res" / "levels" / "level1.ddt").write_bytes(b"\x01\x03")
    assert len({first, second, build_id(d)}) == 3


def test_without_sources_the_executable_is_hashed(tmp_path):
    (tmp_path / BRIDGE_EXE).write_bytes(b"exe v1")
    assert build_id(tmp_path).startswith("deadly-dave-bridge-p")
