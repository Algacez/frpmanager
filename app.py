from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Flask, abort, flash, redirect, render_template, request, url_for

app = Flask(__name__)
app.secret_key = "frpmanager-dev"

ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / "settings.json"


DEFAULT_SETTINGS = {
    "managed_dirs": [],
    "current_dir": "",
    "frpc_path": "",
    "frps_path": "",
    "services": {
        "frps": {"config": ""},
    },
    "frpc_instances": [],
}

FRPC_TEMPLATE = """serverAddr = \"1.1.1.1\"
serverPort = 7000

[[proxies]]
name = \"ssh\"
type = \"tcp\"
localIP = \"127.0.0.1\"
localPort = 22
remotePort = 18022
"""

FRPS_TEMPLATE = """bindPort = 7000
"""

class ServiceState:
    def __init__(self) -> None:
        self.process: Optional[int] = None
        self.desired_running: bool = False
        self.last_exit_code: Optional[int] = None
        self.last_error: str = ""
        self.monitor_thread: Optional[threading.Thread] = None


SERVICE_LOCK = threading.Lock()
FRPS_STATE = ServiceState()
FRPC_STATES: Dict[str, ServiceState] = {}


def load_settings() -> Dict[str, Any]:
    if SETTINGS_FILE.exists():
        with SETTINGS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    settings = {**DEFAULT_SETTINGS, **data}

    if not settings.get("managed_dirs"):
        settings["managed_dirs"] = [str(ROOT)]
    if not settings.get("current_dir"):
        settings["current_dir"] = settings["managed_dirs"][0]

    if "services" not in settings:
        settings["services"] = DEFAULT_SETTINGS["services"].copy()
    settings["services"].setdefault("frps", {"config": ""})
    if not settings["services"]["frps"].get("config"):
        settings["services"]["frps"]["config"] = str(_frps_config_path())

    if "frpc_instances" not in settings or not isinstance(settings["frpc_instances"], list):
        settings["frpc_instances"] = []

    # Migration: old single frpc config -> instance
    legacy = settings.get("services", {}).get("frpc", {}).get("config")
    if legacy:
        if not any(i.get("config") == legacy for i in settings["frpc_instances"]):
            settings["frpc_instances"].append({"id": "default", "config": legacy})
        settings["services"].pop("frpc", None)

    _normalize_instances(settings)
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    _normalize_instances(settings)
    with SETTINGS_FILE.open("w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def _normalize_instances(settings: Dict[str, Any]) -> None:
    unique = {}
    for item in settings.get("frpc_instances", []):
        inst_id = str(item.get("id", "")).strip()
        if not inst_id:
            continue
        unique[inst_id] = {"id": inst_id, "config": str(item.get("config", "")).strip()}
    settings["frpc_instances"] = list(unique.values())


def _frps_config_path() -> Path:
    return ROOT / "frps.toml"


def safe_dir(path_str: str) -> Optional[Path]:
    try:
        p = Path(path_str).expanduser().resolve()
    except Exception:
        return None
    if not p.exists() or not p.is_dir():
        return None
    return p


def resolve_new_dir(path_str: str, base: Path) -> Optional[Path]:
    if not path_str:
        return None
    try:
        p = Path(path_str).expanduser()
        if not p.is_absolute():
            p = base / p
        p = p.resolve()
    except Exception:
        return None
    return p


def current_dir(settings: Dict[str, Any]) -> Path:
    p = safe_dir(settings.get("current_dir", ""))
    if p is None:
        p = safe_dir(settings["managed_dirs"][0])
    return p


def list_toml_files(directory: Path) -> list[Path]:
    return sorted([p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".toml"])


def ensure_in_dir(file_path: Path, directory: Path) -> bool:
    try:
        return directory in file_path.resolve().parents
    except Exception:
        return False


def ensure_in_managed_dirs(file_path: Path, settings: Dict[str, Any]) -> bool:
    try:
        resolved = file_path.resolve()
    except Exception:
        return False
    for d in settings.get("managed_dirs", []):
        base = safe_dir(d)
        if base and base in resolved.parents:
            return True
    return False


def service_path(settings: Dict[str, Any], name: str) -> str:
    return settings.get(f"{name}_path", "")


def build_command(binary: str, config_path: str) -> Optional[list[str]]:
    if not binary:
        return None
    return [binary, "-c", config_path]


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def is_running(state: ServiceState) -> bool:
    if state.process is None:
        return False
    pid = int(state.process)
    return Path(f"/proc/{pid}").exists() if Path("/proc").exists() else _pid_exists(pid)


def _start_process(binary: str, config_path: str, state: ServiceState) -> None:
    if not config_path:
        state.last_error = "请先选择配置文件"
        return
    command = build_command(binary, config_path)
    if not command:
        state.last_error = "请先设置程序路径"
        return
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        state.last_error = f"启动失败: {exc}"
        return
    state.process = proc.pid
    state.last_error = ""


def _stop_process(state: ServiceState) -> None:
    if state.process is None:
        return
    try:
        os.kill(int(state.process), signal.SIGTERM)
    except Exception:
        pass
    state.process = None


def _ensure_frpc_state(instance_id: str) -> ServiceState:
    if instance_id not in FRPC_STATES:
        FRPC_STATES[instance_id] = ServiceState()
    return FRPC_STATES[instance_id]


def _start_frps() -> None:
    settings = load_settings()
    config_path = settings["services"]["frps"].get("config", "")
    binary = service_path(settings, "frps")
    _start_process(binary, config_path, FRPS_STATE)


def _start_frpc(instance_id: str) -> None:
    settings = load_settings()
    instance = _find_instance(settings, instance_id)
    if not instance:
        return
    binary = service_path(settings, "frpc")
    _start_process(binary, instance.get("config", ""), _ensure_frpc_state(instance_id))


def _find_instance(settings: Dict[str, Any], instance_id: str) -> Optional[Dict[str, str]]:
    for item in settings.get("frpc_instances", []):
        if item.get("id") == instance_id:
            return item
    return None


def _monitor_frps() -> None:
    while True:
        time.sleep(1.5)
        with SERVICE_LOCK:
            if not FRPS_STATE.desired_running:
                if FRPS_STATE.process is not None:
                    _stop_process(FRPS_STATE)
                continue
            if not is_running(FRPS_STATE):
                FRPS_STATE.process = None
                _start_frps()


def _monitor_frpc(instance_id: str) -> None:
    state = _ensure_frpc_state(instance_id)
    while True:
        time.sleep(1.5)
        with SERVICE_LOCK:
            if not state.desired_running:
                if state.process is not None:
                    _stop_process(state)
                continue
            if not is_running(state):
                state.process = None
                _start_frpc(instance_id)


def _ensure_monitor_frps() -> None:
    if FRPS_STATE.monitor_thread and FRPS_STATE.monitor_thread.is_alive():
        return
    t = threading.Thread(target=_monitor_frps, daemon=True)
    FRPS_STATE.monitor_thread = t
    t.start()


def _ensure_monitor_frpc(instance_id: str) -> None:
    state = _ensure_frpc_state(instance_id)
    if state.monitor_thread and state.monitor_thread.is_alive():
        return
    t = threading.Thread(target=_monitor_frpc, args=(instance_id,), daemon=True)
    state.monitor_thread = t
    t.start()


def _validate_instance_id(instance_id: str) -> bool:
    if not instance_id:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    return all(ch in allowed for ch in instance_id)


def _sanitize_instance_id(raw: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    cleaned = "".join(ch if ch in allowed else "-" for ch in raw)
    return cleaned.strip("-_")


def _unique_instance_id(settings: Dict[str, Any], base_id: str) -> str:
    existing = {i.get("id") for i in settings.get("frpc_instances", [])}
    if base_id not in existing:
        return base_id
    idx = 2
    while f"{base_id}-{idx}" in existing:
        idx += 1
    return f"{base_id}-{idx}"


@app.route("/")
def index() -> str:
    settings = load_settings()
    directory = current_dir(settings)
    files = list_toml_files(directory)
    return render_template(
        "index.html",
        settings=settings,
        directory=directory,
        files=files,
        states=_service_status(),
    )


@app.route("/set-dir", methods=["POST"])
def set_dir() -> Any:
    settings = load_settings()
    path_str = request.form.get("dir", "")
    p = safe_dir(path_str)
    if p is None:
        flash("目录无效", "error")
        return redirect(url_for("index"))
    p_str = str(p)
    if p_str not in settings["managed_dirs"]:
        settings["managed_dirs"].append(p_str)
    settings["current_dir"] = p_str
    save_settings(settings)
    flash("已切换目录", "success")
    return redirect(url_for("index"))


@app.route("/create-dir", methods=["POST"])
def create_dir() -> Any:
    settings = load_settings()
    base = current_dir(settings)
    raw = request.form.get("new_dir", "").strip()
    p = resolve_new_dir(raw, base)
    if p is None:
        flash("目录无效", "error")
        return redirect(url_for("index"))
    if p.exists():
        flash("目录已存在", "error")
        return redirect(url_for("index"))
    try:
        p.mkdir(parents=True, exist_ok=False)
    except Exception as exc:
        flash(f"创建失败: {exc}", "error")
        return redirect(url_for("index"))
    p_str = str(p)
    if p_str not in settings["managed_dirs"]:
        settings["managed_dirs"].append(p_str)
    settings["current_dir"] = p_str
    save_settings(settings)
    flash("已创建并切换目录", "success")
    return redirect(url_for("index"))


@app.route("/remove-dir", methods=["POST"])
def remove_dir() -> Any:
    settings = load_settings()
    path_str = request.form.get("dir", "")
    if path_str in settings["managed_dirs"]:
        settings["managed_dirs"].remove(path_str)
    if not settings["managed_dirs"]:
        settings["managed_dirs"] = [str(ROOT)]
    if settings["current_dir"] == path_str:
        settings["current_dir"] = settings["managed_dirs"][0]
    save_settings(settings)
    flash("已移除目录", "success")
    return redirect(url_for("index"))


@app.route("/create-file", methods=["POST"])
def create_file() -> Any:
    settings = load_settings()
    directory = current_dir(settings)
    name = request.form.get("new_file", "").strip()
    if not name:
        flash("文件名不能为空", "error")
        return redirect(url_for("index"))
    if Path(name).name != name:
        flash("文件名无效", "error")
        return redirect(url_for("index"))
    if not name.lower().endswith(".toml"):
        name = f"{name}.toml"
    file_path = (directory / name).resolve()
    if file_path.exists():
        flash("文件已存在", "error")
        return redirect(url_for("index"))
    if not ensure_in_dir(file_path, directory):
        abort(403)
    try:
        file_path.write_text(FRPC_TEMPLATE, encoding="utf-8")
    except Exception as exc:
        flash(f"创建失败: {exc}", "error")
        return redirect(url_for("index"))
    flash("已创建配置文件", "success")
    return redirect(url_for("edit_file", filename=name))


@app.route("/delete-file", methods=["POST"])
def delete_file() -> Any:
    settings = load_settings()
    directory = current_dir(settings)
    name = request.form.get("filename", "").strip()
    file_path = (directory / name).resolve()
    if not file_path.exists() or not ensure_in_dir(file_path, directory):
        abort(404)
    try:
        file_path.unlink()
    except Exception as exc:
        flash(f"删除失败: {exc}", "error")
        return redirect(url_for("index"))
    # Clear bindings if any instance uses it
    for inst in settings.get("frpc_instances", []):
        if inst.get("config") and Path(inst["config"]).resolve() == file_path:
            inst["config"] = ""
    save_settings(settings)
    flash("已删除配置文件", "success")
    return redirect(url_for("index"))


@app.route("/edit/<path:filename>")
def edit_file(filename: str) -> str:
    settings = load_settings()
    directory = current_dir(settings)
    file_path = (directory / filename).resolve()
    if not file_path.exists() or not ensure_in_dir(file_path, directory):
        abort(404)
    content = file_path.read_text(encoding="utf-8")
    return render_template(
        "edit.html",
        settings=settings,
        directory=directory,
        filename=filename,
        content=content,
        states=_service_status(),
    )


@app.route("/save/<path:filename>", methods=["POST"])
def save_file(filename: str) -> Any:
    settings = load_settings()
    directory = current_dir(settings)
    file_path = (directory / filename).resolve()
    if not ensure_in_dir(file_path, directory):
        abort(403)
    content = request.form.get("content", "")
    file_path.write_text(content, encoding="utf-8")
    flash("已保存", "success")

    with SERVICE_LOCK:
        # Restart frps if using this config
        frps_cfg = settings["services"]["frps"].get("config", "")
        if frps_cfg and Path(frps_cfg).resolve() == file_path and FRPS_STATE.desired_running:
            _stop_process(FRPS_STATE)
            _start_frps()

        # Restart frpc instances using this config
        for inst in settings.get("frpc_instances", []):
            cfg = inst.get("config", "")
            if cfg and Path(cfg).resolve() == file_path:
                state = _ensure_frpc_state(inst["id"])
                if state.desired_running:
                    _stop_process(state)
                    _start_frpc(inst["id"])

    return redirect(url_for("edit_file", filename=filename))


@app.route("/frps/edit")
def frps_edit() -> str:
    settings = load_settings()
    file_path = _frps_config_path()
    if not file_path.exists():
        try:
            file_path.write_text(FRPS_TEMPLATE, encoding="utf-8")
        except Exception as exc:
            flash(f"创建 frps 配置失败: {exc}", "error")
            return redirect(url_for("service_page"))
    content = file_path.read_text(encoding="utf-8")
    return render_template(
        "frps_edit.html",
        settings=settings,
        filename=file_path.name,
        content=content,
        states=_service_status(),
    )


@app.route("/frps/save", methods=["POST"])
def frps_save() -> Any:
    settings = load_settings()
    file_path = _frps_config_path()
    content = request.form.get("content", "")
    file_path.write_text(content, encoding="utf-8")
    settings["services"]["frps"]["config"] = str(file_path)
    save_settings(settings)
    flash("frps 配置已保存", "success")
    with SERVICE_LOCK:
        if FRPS_STATE.desired_running:
            _stop_process(FRPS_STATE)
            _start_frps()
    return redirect(url_for("frps_edit"))


@app.route("/service")
def service_page() -> str:
    settings = load_settings()
    directory = current_dir(settings)
    return render_template(
        "service.html",
        settings=settings,
        directory=directory,
        states=_service_status(),
        files=list_toml_files(directory),
        frps_config_path=_frps_config_path(),
    )


@app.route("/service/update", methods=["POST"])
def service_update() -> Any:
    settings = load_settings()
    settings["frpc_path"] = request.form.get("frpc_path", "").strip()
    settings["frps_path"] = request.form.get("frps_path", "").strip()
    save_settings(settings)
    flash("已保存程序路径", "success")
    return redirect(url_for("service_page"))


@app.route("/service/frps/set-config", methods=["POST"])
def frps_set_config() -> Any:
    settings = load_settings()
    config_path = _frps_config_path()
    settings["services"]["frps"]["config"] = str(config_path)
    save_settings(settings)
    flash("已绑定 frps 配置", "success")
    return redirect(url_for("service_page"))


@app.route("/service/frps/start", methods=["POST"])
def frps_start() -> Any:
    with SERVICE_LOCK:
        FRPS_STATE.desired_running = True
        _ensure_monitor_frps()
        _start_frps()
    flash("frps 已启动/守护", "success")
    return redirect(url_for("service_page"))


@app.route("/service/frps/stop", methods=["POST"])
def frps_stop() -> Any:
    with SERVICE_LOCK:
        FRPS_STATE.desired_running = False
        _stop_process(FRPS_STATE)
    flash("frps 已停止", "success")
    return redirect(url_for("service_page"))


@app.route("/service/frps/restart", methods=["POST"])
def frps_restart() -> Any:
    with SERVICE_LOCK:
        FRPS_STATE.desired_running = True
        _stop_process(FRPS_STATE)
        _ensure_monitor_frps()
        _start_frps()
    flash("frps 已重启", "success")
    return redirect(url_for("service_page"))


@app.route("/service/frpc/add", methods=["POST"])
def frpc_add() -> Any:
    settings = load_settings()
    instance_id = request.form.get("instance_id", "").strip()
    config_path = request.form.get("config_path", "").strip()
    if not instance_id:
        if not config_path:
            flash("请至少填写实例名或选择配置文件", "error")
            return redirect(url_for("service_page"))
        instance_id = Path(config_path).expanduser().name
        if instance_id.endswith(".toml"):
            instance_id = instance_id[:-5]
        instance_id = _sanitize_instance_id(instance_id)
        if not instance_id:
            flash("无法从配置文件名生成实例名，请手动填写", "error")
            return redirect(url_for("service_page"))
    if not _validate_instance_id(instance_id):
        flash("实例名仅支持字母/数字/-/_", "error")
        return redirect(url_for("service_page"))
    if _find_instance(settings, instance_id):
        instance_id = _unique_instance_id(settings, instance_id)
    if config_path:
        cfg = Path(config_path).expanduser().resolve()
        if not cfg.exists():
            flash("配置文件不存在", "error")
            return redirect(url_for("service_page"))
        config_path = str(cfg)
    settings["frpc_instances"].append({"id": instance_id, "config": config_path})
    save_settings(settings)
    flash("已添加实例", "success")
    return redirect(url_for("service_page"))


@app.route("/service/frpc/remove", methods=["POST"])
def frpc_remove() -> Any:
    settings = load_settings()
    instance_id = request.form.get("instance_id", "").strip()
    settings["frpc_instances"] = [i for i in settings["frpc_instances"] if i.get("id") != instance_id]
    save_settings(settings)
    with SERVICE_LOCK:
        state = FRPC_STATES.pop(instance_id, None)
        if state:
            state.desired_running = False
            _stop_process(state)
    flash("已移除实例", "success")
    return redirect(url_for("service_page"))


@app.route("/service/frpc/set-config", methods=["POST"])
def frpc_set_config() -> Any:
    settings = load_settings()
    instance_id = request.form.get("instance_id", "").strip()
    config_path = request.form.get("config_path", "").strip()
    instance = _find_instance(settings, instance_id)
    if not instance:
        abort(404)
    if config_path:
        cfg = Path(config_path).expanduser().resolve()
        if not cfg.exists():
            flash("配置文件不存在", "error")
            return redirect(url_for("service_page"))
        instance["config"] = str(cfg)
        save_settings(settings)
        flash("已更新配置文件", "success")
    return redirect(url_for("service_page"))


@app.route("/service/frpc/delete-config", methods=["POST"])
def frpc_delete_config() -> Any:
    settings = load_settings()
    instance_id = request.form.get("instance_id", "").strip()
    instance = _find_instance(settings, instance_id)
    if not instance:
        abort(404)
    cfg_path = instance.get("config", "")
    if not cfg_path:
        flash("该实例未设置配置文件", "error")
        return redirect(url_for("service_page"))
    file_path = Path(cfg_path)
    if not file_path.exists():
        instance["config"] = ""
        save_settings(settings)
        flash("配置文件已不存在，已清除绑定", "success")
        return redirect(url_for("service_page"))
    if not ensure_in_managed_dirs(file_path, settings):
        flash("仅允许删除已管理目录内的配置文件", "error")
        return redirect(url_for("service_page"))
    try:
        file_path.unlink()
    except Exception as exc:
        flash(f"删除失败: {exc}", "error")
        return redirect(url_for("service_page"))
    instance["config"] = ""
    save_settings(settings)
    with SERVICE_LOCK:
        state = _ensure_frpc_state(instance_id)
        if state.desired_running:
            state.desired_running = False
            _stop_process(state)
    flash("已删除配置文件并停止实例", "success")
    return redirect(url_for("service_page"))


@app.route("/service/frpc/start", methods=["POST"])
def frpc_start() -> Any:
    settings = load_settings()
    instance_id = request.form.get("instance_id", "").strip()
    if not _find_instance(settings, instance_id):
        abort(404)
    with SERVICE_LOCK:
        state = _ensure_frpc_state(instance_id)
        state.desired_running = True
        _ensure_monitor_frpc(instance_id)
        _start_frpc(instance_id)
    flash("frpc 实例已启动/守护", "success")
    return redirect(url_for("service_page"))


@app.route("/service/frpc/stop", methods=["POST"])
def frpc_stop() -> Any:
    instance_id = request.form.get("instance_id", "").strip()
    with SERVICE_LOCK:
        state = _ensure_frpc_state(instance_id)
        state.desired_running = False
        _stop_process(state)
    flash("frpc 实例已停止", "success")
    return redirect(url_for("service_page"))


@app.route("/service/frpc/restart", methods=["POST"])
def frpc_restart() -> Any:
    instance_id = request.form.get("instance_id", "").strip()
    with SERVICE_LOCK:
        state = _ensure_frpc_state(instance_id)
        state.desired_running = True
        _stop_process(state)
        _ensure_monitor_frpc(instance_id)
        _start_frpc(instance_id)
    flash("frpc 实例已重启", "success")
    return redirect(url_for("service_page"))


def _service_status() -> Dict[str, Any]:
    settings = load_settings()
    frpc_list = []
    for inst in settings.get("frpc_instances", []):
        instance_id = inst.get("id", "")
        state = _ensure_frpc_state(instance_id)
        frpc_list.append(
            {
                "id": instance_id,
                "running": is_running(state),
                "desired": state.desired_running,
                "config": inst.get("config", ""),
                "path": settings.get("frpc_path", ""),
                "error": state.last_error,
            }
        )

    return {
        "frps": {
            "running": is_running(FRPS_STATE),
            "desired": FRPS_STATE.desired_running,
            "config": settings["services"]["frps"].get("config", ""),
            "path": settings.get("frps_path", ""),
            "error": FRPS_STATE.last_error,
        },
        "frpc_instances": frpc_list,
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=True)
