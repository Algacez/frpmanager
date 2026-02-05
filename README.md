# FRP 文件管理 WebUI

这是一个简单的 WebUI，用于：

- 管理指定目录下的 `toml` 配置文件
- 切换/管理目录
- 直接编辑并保存配置文件
- 设置 `frpc` / `frps` 程序路径
- 使用指定配置文件启动并守护进程，配置更新后自动重启

## 运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

打开 `http://127.0.0.1:5000`。

## 说明

- 管理目录与服务设置保存在 `settings.json`。
- 点击保存后，如果服务正在使用当前配置文件，会自动重启。
- 启动采用守护线程自动重启（简单版）。

## 注意

- 这是轻量级实现，适合本地使用。
- 若要长期运行，请考虑使用系统服务（systemd/launchd）进行外部守护。
