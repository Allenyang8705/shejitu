"""config.py 单元测试：校验逻辑必须与 config.yaml.example 的实际结构对齐。"""

from pathlib import Path

import pytest
import yaml

from shared.config import get_config, load_config, reset_config
from shared.errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = ROOT / "config.yaml.example"

BASE_CONFIG = {
    "team_name": "测试团队",
    "repos": ["org/repo"],
    "chats": [{"chat_id": "oc_test", "name": "项目群"}],
    "lark_project_id": "proj-001",
    "message_keywords": ["进度"],
    "sensitive_keywords": ["薪资"],
    "members": [{"name": "张三", "github": "zhangsan", "lark": "zhangsan@company.com"}],
}


def _write_config(tmp_path: Path, **overrides: object) -> Path:
    """生成一份合法配置并按参数覆盖字段，用于各类校验场景。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({**BASE_CONFIG, **overrides}, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def test_load_example_succeeds():
    """config.yaml.example 必须能通过校验，防止示例结构与校验逻辑脱节。"""
    config = load_config(EXAMPLE_PATH)
    assert config.team_name == "示例团队"
    assert config.repos == ["your-org/your-repo"]
    assert config.chats[0].chat_id.startswith("oc_")
    assert config.lark_project_id
    assert config.message_keywords
    assert config.sensitive_keywords == ["薪资", "绩效", "裁员"]
    assert len(config.members) == 2
    assert config.members[0].github == "zhangsan"


def test_load_valid_config_maps_types(tmp_path):
    path = _write_config(tmp_path)
    config = load_config(path)
    assert config.team_name == "测试团队"
    assert config.chats[0].chat_id == "oc_test"
    assert config.chats[0].name == "项目群"
    assert config.members[0].name == "张三"
    assert config.members[0].lark == "zhangsan@company.com"


def test_missing_file_raises():
    with pytest.raises(ConfigError, match="不存在"):
        load_config("/nonexistent/config.yaml")


def test_missing_required_key_names_it(tmp_path):
    data = {key: value for key, value in BASE_CONFIG.items() if key != "team_name"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="team_name"):
        load_config(path)


def test_empty_team_name_names_field(tmp_path):
    path = _write_config(tmp_path, team_name="  ")
    with pytest.raises(ConfigError, match="team_name"):
        load_config(path)


def test_members_not_list(tmp_path):
    path = _write_config(tmp_path, members="张三")
    with pytest.raises(ConfigError, match="members"):
        load_config(path)


def test_member_missing_github_names_field(tmp_path):
    path = _write_config(tmp_path, members=[{"name": "张三", "lark": "zs@c.com"}])
    with pytest.raises(ConfigError, match="github"):
        load_config(path)


def test_empty_members_rejected(tmp_path):
    path = _write_config(tmp_path, members=[])
    with pytest.raises(ConfigError, match="至少"):
        load_config(path)


def test_chat_missing_name_names_field(tmp_path):
    path = _write_config(tmp_path, chats=[{"chat_id": "oc_1"}])
    with pytest.raises(ConfigError, match="name"):
        load_config(path)


def test_yaml_syntax_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("team_name: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="解析"):
        load_config(path)


def test_root_not_mapping(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="键值映射"):
        load_config(path)


def test_get_config_caches_until_reset(tmp_path):
    reset_config()
    path = _write_config(tmp_path)
    first = get_config(path)
    assert get_config(path) is first
    reset_config()
    assert get_config(path) is not first
    reset_config()


def test_work_item_types_default_when_missing(tmp_path):
    """lark_work_item_types 未配置时回退到平台内置三类型。"""
    path = _write_config(tmp_path)
    assert load_config(path).lark_work_item_types == ["task", "bug", "story"]
