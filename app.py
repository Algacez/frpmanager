from __future__ import annotations

import json
import os
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
        "frpc": {"config": ""},
        "frps": {"config": ""},
    },
}


class ServiceState:
    def __init__(self) -> None:
        self.process: Optional[Any] = None
        self.desired_running: bool = False
        self.last_exit_code: Optional[int] = None
        self.last_error: str = ""
        self.monitor_thread: Optional[threading.Thread] = None


SERVICE_LOCK = threading.Lock()
SERVICE_STATE: Dict[str, ServiceState] = {
    "frpc": ServiceState(),
    "frps": ServiceState(),
}


def load_settings() -> Dict[str, Any]:
    if SETTINGS_FILE.exists():
        with SETTINGS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    settings = {**DEFAULT_SETTINGS, **data}
    if not settings["managed_dirs"]:
        settings["managed_dirs"] = [str(ROOT)]
    if not settings["current_dir"]:
        settings["current_dir"] = settings["managed_dirs"][0]
    if "services" not in settings:
        settings["services"] = DEFAULT_SETTINGS["services"].copy()
    for svc in ("frpc", "frps"):
        settings["services"].setdefault(svc, {"config": ""})
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    with SETTINGS_FILE.open("w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def safe_dir(path_str: str) -> Optional[Path]:
    try:
        p = Path(path_str).expanduser().resolve()
    except Exception:
        return None
    if not p.exists() or not p.is_dir():
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


def service_path(settings: Dict[str, Any], name: str) -> str:
    return settings.get(f"{name}_path", "")


def build_command(settings: Dict[str, Any], name: str, config_path: str) -> Optional[list[str]]:
    binary = service_path(settings, name)
    if not binary:
        return None
    return [binary, "-c", config_path]


def start_process(name: str) -> None:
    settings = load_settings()
    state = SERVICE_STATE[name]
    config_path = settings["services"][name].get("config", "")
    if not config_path:
        state.last_error = "请先选择配置文件"
        return
    command = build_command(settings, name, config_path)
    if not command:
        state.last_error = "请先设置程序路径"
        return
    try:
        proc = os.spawnv(os.P_NOWAIT, command[0], command)
    except Exception as exc:
        state.last_error = f"启动失败: {exc}"
        return
    state.process = proc
    state.last_error = ""


def is_running(state: ServiceState) -> bool:
    if state.process is None:
        return False
    try:
        pid = int(state.process)
    except Exception:
        return False
    return Path(f"/proc/{pid}").exists() if Path("/proc").exists() else _pid_exists(pid)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_process(name: str) -> None:
    state = SERVICE_STATE[name]
    if state.process is None:
        return
    try:
        os.kill(int(state.process), 15)
    except Exception:
        pass
    state.process = None


def monitor_service(name: str) -> None:
    state = SERVICE_STATE[name]
    while True:
        time.sleep(1.5)
        with SERVICE_LOCK:
            if not state.desired_running:
                if state.process is not None:
                    stop_process(name)
                continue
            if not is_running(state):
                state.process = None
                start_process(name)


def ensure_monitor(name: str) -> None:
    state = SERVICE_STATE[name]
    if state.monitor_thread and state.monitor_thread.is_alive():
        return
    t = threading.Thread(target=monitor_service, args=(name,), daemon=True)
    state.monitor_thread = t
    t.start()


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

    # Restart services using this config
    for name in ("frpc", "frps"):
        cfg = settings["services"][name].get("config", "")
        if cfg and Path(cfg).resolve() == file_path:
            with SERVICE_LOCK:
                state = SERVICE_STATE[name]
                if state.desired_running:
                    stop_process(name)
                    start_process(name)
    return redirect(url_for("edit_file", filename=filename))


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
    )


@app.route("/service/update", methods=["POST"])
def service_update() -> Any:
    settings = load_settings()
    settings["frpc_path"] = request.form.get("frpc_path", "").strip()
    settings["frps_path"] = request.form.get("frps_path", "").strip()
    save_settings(settings)
    flash("已保存程序路径", "success")
    return redirect(url_for("service_page"))


@app.route("/service/set-config", methods=["POST"])
def service_set_config() -> Any:
    settings = load_settings()
    name = request.form.get("name", "")
    config_path = request.form.get("config_path", "").strip()
    if name not in ("frpc", "frps"):
        abort(400)
    if config_path:
        cfg = Path(config_path).expanduser().resolve()
        if not cfg.exists():
            flash("配置文件不存在", "error")
            return redirect(url_for("service_page"))
        settings["services"][name]["config"] = str(cfg)
        save_settings(settings)
        flash("已更新配置文件", "success")
    return redirect(url_for("service_page"))


@app.route("/service/start", methods=["POST"])
def service_start() -> Any:
    settings = load_settings()
    name = request.form.get("name", "")
    if name not in ("frpc", "frps"):
        abort(400)
    with SERVICE_LOCK:
        state = SERVICE_STATE[name]
        state.desired_running = True
        ensure_monitor(name)
        start_process(name)
    flash("已启动/守护", "success")
    return redirect(url_for("service_page"))


@app.route("/service/stop", methods=["POST"])
def service_stop() -> Any:
    name = request.form.get("name", "")
    if name not in ("frpc", "frps"):
        abort(400)
    with SERVICE_LOCK:
        state = SERVICE_STATE[name]
        state.desired_running = False
        stop_process(name)
    flash("已停止", "success")
    return redirect(url_for("service_page"))


@app.route("/service/restart", methods=["POST"])
def service_restart() -> Any:
    settings = load_settings()
    name = request.form.get("name", "")
    if name not in ("frpc", "frps"):
        abort(400)
    with SERVICE_LOCK:
        state = SERVICE_STATE[name]
        state.desired_running = True
        stop_process(name)
        ensure_monitor(name)
        start_process(name)
    flash("已重启", "success")
    return redirect(url_for("service_page"))


def _service_status() -> Dict[str, Dict[str, Any]]:
    settings = load_settings()
    status: Dict[str, Dict[str, Any]] = {}
    for name in ("frpc", "frps"):
        state = SERVICE_STATE[name]
        status[name] = {
            "running": is_running(state),
            "desired": state.desired_running,
            "config": settings["services"][name].get("config", ""),
            "path": settings.get(f"{name}_path", ""),
            "error": state.last_error,
        }
    return status


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=True)
