"""Keep the coordinator's config in the scheduler-owned settings table."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 256 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
MODEL = re.compile(r"[A-Za-z0-9._/-]{1,120}\Z")
KINDS = {"lead", "developer", "reviewer", "specialist"}
AGENTS = {"codex", "dsh", "antigravity"}
EFFORTS = {"default", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


def default_config():
    return {
        "schemaVersion": SCHEMA_VERSION,
        "roles": [
            {"id": "lead", "kind": "lead", "name": "主控", "duties": "明确目标、拆分任务并核对成果。"},
            {"id": "developer", "kind": "developer", "name": "开发", "duties": "接单后执行，取得写入资格后才修改文件。"},
            {"id": "reviewer", "kind": "reviewer", "name": "审查", "duties": "独立只读核对交付物与验证结果。"},
        ],
        "teams": [
            {
                "id": "default",
                "name": "日常开发团队",
                "acceptance": "chat",
                "members": [
                    {"id": kind, "role": kind, "agent": "codex", "model": "default", "write_allowed": False}
                    for kind in ("lead", "developer", "reviewer")
                ],
            }
        ],
        "settings": {"python": "", "hc": "", "cli": {}},
    }


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _get(db, key):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _put(db, key, value):
    db.execute(
        "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, _json(value)),
    )


def _object(value, known_keys, label):
    if not isinstance(value, dict) or value.keys() - known_keys:
        raise ValueError(f"{label}包含无效字段")
    return value


def _list(value, label, maximum=50):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{label}须为 1 至 {maximum} 项")
    return value


def _id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("编号须为 1 至 80 个字母、数字、下划线或连字符")
    return value


def _text(value, label, length, required=False):
    if not isinstance(value, str) or len(value) > length or "\0" in value:
        raise ValueError(f"{label}须为不超过 {length} 字的文本")
    if required and not value.strip():
        raise ValueError(f"{label}不能为空")
    return value.strip()


def _boolean(value, label):
    if type(value) is not bool:
        raise ValueError(f"{label}须为明确的 true 或 false")
    return value


def _unique(items, label):
    selection = {item["id"].casefold() for item in items}
    if len(selection) != len(items):
        raise ValueError(f"{label}编号重复")


def _role(source):
    _object(source, {"id", "name", "kind", "duties", "deliverables", "reporting", "revision", "archived"}, "角色")
    role_id = _id(source.get("id"))
    kind = source.get("kind", role_id if role_id in KINDS else "developer")
    revision = source.get("revision", 1)
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("角色类型无效")
    if type(revision) is not int or not 1 <= revision <= 10**9:
        raise ValueError("角色版本无效")
    return {
        "id": role_id,
        "kind": kind,
        "name": _text(source.get("name"), "角色名称", 120, required=True),
        "duties": _text(source.get("duties", ""), "职责", 8000),
        "deliverables": _text(source.get("deliverables", ""), "交付物", 4000),
        "reporting": _text(source.get("reporting", ""), "回报要求", 4000),
        "revision": revision,
        "archived": _boolean(source.get("archived", False), "角色归档状态"),
    }


def _member(source, roles):
    _object(source, {"id", "role", "kind", "agent", "model", "effort", "write_allowed", "execution_mode"}, "成员")
    member_id = _id(source.get("id"))
    role_id = _id(source.get("role"))
    if role_id not in roles:
        raise ValueError(f"成员 {member_id} 引用了不存在的角色 {role_id}")
    kind = roles[role_id]["kind"]
    if source.get("kind", kind) != kind:
        raise ValueError(f"成员 {member_id} 类型与角色不一致")
    agent = source.get("agent")
    model = source.get("model", "default")
    effort = source.get("effort", "default")
    if not isinstance(agent, str) or agent not in AGENTS:
        raise ValueError(f"成员 {member_id} 的 CLI 无效")
    if not isinstance(model, str) or not MODEL.fullmatch(model) or not model[0].isalnum():
        raise ValueError(f"成员 {member_id} 的模型无效")
    if not isinstance(effort, str) or effort not in EFFORTS:
        raise ValueError(f"成员 {member_id} 的思考强度无效")
    write_allowed = _boolean(source.get("write_allowed", False), "成员写入资格")
    execution_mode = source.get("execution_mode", "legacy")
    if execution_mode not in ("legacy", "approval", "full_auto"):
        raise ValueError(f"成员 {member_id} 的执行权限档位无效")
    if write_allowed and kind not in {"developer", "specialist"}:
        raise ValueError("写入资格仅可配置给开发或攻坚成员")
    return {
        "id": member_id, "role": role_id, "kind": kind, "agent": agent,
        "model": model, "effort": effort, "write_allowed": write_allowed, "execution_mode": execution_mode,
    }


def _team(source, roles):
    _object(source, {"id", "name", "archived", "acceptance", "members"}, "团队")
    team_id = _id(source.get("id"))
    archived = _boolean(source.get("archived", False), "团队归档状态")
    members = [_member(item, roles) for item in _list(source.get("members"), "团队成员", 16)]
    _unique(members, "成员")
    if sum(member["kind"] == "lead" for member in members) != 1:
        raise ValueError(f"团队 {team_id} 须有一位主控")
    if not any(member["kind"] == "reviewer" for member in members):
        raise ValueError(f"团队 {team_id} 须有一位独立审查成员")
    if not archived and any(roles[member["role"]]["archived"] for member in members):
        raise ValueError(f"活跃团队 {team_id} 不能引用归档角色")
    acceptance = source.get("acceptance", "chat")
    if acceptance not in ("chat", "local"):
        raise ValueError("验收模式须为 chat 或 local")
    return {
        "id": team_id, "name": _text(source.get("name"), "团队名称", 120, required=True),
        "archived": archived, "acceptance": acceptance, "members": members,
    }


def normalize_config(config):
    _object(config, {"schemaVersion", "roles", "teams", "settings"}, "配置")
    if type(config.get("schemaVersion")) is not int or config["schemaVersion"] != SCHEMA_VERSION:
        raise ValueError("配置版本不受支持")
    roles = [_role(role) for role in _list(config.get("roles"), "角色")]
    _unique(roles, "角色")
    role_map = {role["id"]: role for role in roles}
    teams = [_team(team, role_map) for team in _list(config.get("teams"), "团队")]
    _unique(teams, "团队")
    settings = _object(config.get("settings", {}), {"python", "hc", "cli"}, "运行环境")
    cli = _object(settings.get("cli", {}), AGENTS, "CLI 路径")
    normalized = {
        "schemaVersion": SCHEMA_VERSION,
        "roles": roles,
        "teams": teams,
        "settings": {
            "python": _text(settings.get("python", ""), "Python 路径", 2048),
            "hc": _text(settings.get("hc", ""), "hc 路径", 2048),
            "cli": {agent: _text(path, "CLI 路径", 2048) for agent, path in cli.items() if path != ""},
        },
    }
    if len(_json(normalized).encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("配置内容超过 256 KiB")
    return normalized


def load_config(db):
    value = _get(db, "config")
    return normalize_config(default_config() if value is None else value)


def save_config(db, config):
    value = normalize_config(config)
    _put(db, "config", value)
    return value


def _digest(config):
    return hashlib.sha256(_json(config).encode("utf-8")).hexdigest()


def _legacy_config(path):
    with path.open("rb") as file:
        raw = file.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("旧配置超过 1 MiB")
    old = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(old, dict) or type(old.get("schemaVersion")) is not int or old["schemaVersion"] != 1:
        raise ValueError("旧团队管理器配置版本不受支持")
    config = default_config()
    config["roles"] = old.get("roles")
    config["teams"] = []
    for team in _list(old.get("teams"), "旧团队"):
        if not isinstance(team, dict):
            raise ValueError("旧团队格式无效")
        members = []
        for member in _list(team.get("members"), "旧团队成员", 16):
            if not isinstance(member, dict):
                raise ValueError("旧成员格式无效")
            selected = {key: member[key] for key in ("id", "role", "agent", "model", "effort") if key in member}
            members.append({**selected, "write_allowed": False})
        config["teams"].append({
            "id": team.get("id"), "name": team.get("name"), "archived": team.get("archived", False),
            "acceptance": "chat", "members": members,
        })
    return normalize_config(config)


def preview_import(db):
    path = Path.home() / "HerdrTeamManager" / "config.json"
    if not path.is_file():
        raise ValueError("没有找到旧版团队管理器的 config.json")
    current = load_config(db)
    candidate = _legacy_config(path)
    candidate["settings"] = current["settings"]
    preview = {
        "preview_id": uuid.uuid4().hex,
        "source": str(path),
        "base_digest": _digest(current),
        "config": candidate,
        "expires_at": time.time() + 1800,
        "notes": [
            "应用后替换本插件的角色和团队配置。",
            "导入的成员均须重新确认写入权限，旧攻坚授权和项目运行记录继续由原插件处理。",
            "旧角色职责中如有旧信箱命令，请在启动团队前更新。",
        ],
    }
    _put(db, "import_preview", preview)
    return preview


def apply_import(db, preview_id):
    preview = _get(db, "import_preview")
    if not preview or preview["preview_id"] != preview_id or preview["expires_at"] < time.time():
        raise ValueError("导入预览已失效，请重新生成预览")
    if preview["base_digest"] != _digest(load_config(db)):
        raise ValueError("团队配置已变动，请重新核对导入预览")
    result = save_config(db, preview["config"])
    _put(db, "import_preview", None)
    return result
