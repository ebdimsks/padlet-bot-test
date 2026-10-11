import os
import sys
import json
import logging
import threading
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, render_template, session
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

# Windows環境でのコンソールUTF-8出力対策
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from padlet_client import PadletClient
from monitor_engine import SpamMonitorEngine

load_dotenv(os.path.join(BASE_DIR, ".env"))
load_dotenv()

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

import secrets

CONFIG_FILE = os.path.join(BASE_DIR, "bot_config.json")
_config_lock = threading.Lock()
_config_cache = None

# -------------------------------------------------------------
# セッションシークレットキーの個別化 (デプロイごとに異なる安全なキー)
# -------------------------------------------------------------
def _get_or_create_secret_key() -> str:
    env_secret = os.environ.get("SECRET_KEY", "").strip()
    if env_secret:
        return env_secret
    secret_file = os.path.join(BASE_DIR, ".session_secret")
    if os.path.exists(secret_file):
        try:
            with open(secret_file, "r", encoding="utf-8") as f:
                k = f.read().strip()
                if k:
                    return k
        except Exception:
            pass
    # 未設定時は暗号学的に安全な64文字の個別ランダムキーを生成
    new_key = secrets.token_hex(32)
    try:
        with open(secret_file, "w", encoding="utf-8") as f:
            f.write(new_key)
    except Exception:
        pass
    return new_key

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = _get_or_create_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=False,
)

# -------------------------------------------------------------
# 管理者パスワード管理 (環境変数最優先 & 未設定時は個別ランダム生成)
# -------------------------------------------------------------
def _get_or_create_admin_password() -> tuple[str, bool]:
    """(パスワード文字列, 環境変数から取得したかどうか) を返す"""
    env_pass = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env_pass:
        return env_pass, True

    # 環境変数が設定されていない場合、全ユーザー共通の固定値にせず個別の安全なパスワードを自動生成
    pass_file = os.path.join(BASE_DIR, ".generated_admin_password")
    if os.path.exists(pass_file):
        try:
            with open(pass_file, "r", encoding="utf-8") as f:
                saved = f.read().strip()
                if saved:
                    return saved, False
        except Exception:
            pass

    generated_pass = secrets.token_urlsafe(12)
    try:
        with open(pass_file, "w", encoding="utf-8") as f:
            f.write(generated_pass)
    except Exception:
        pass
    return generated_pass, False

def get_admin_password() -> str:
    pwd, _ = _get_or_create_admin_password()
    return pwd

# ログバッファ（最新100件）
_bot_logs = []
_logs_lock = threading.Lock()


def load_config() -> dict:
    global _config_cache
    with _config_lock:
        if _config_cache is None:
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                        _config_cache = json.load(f)
                except Exception as e:
                    logger.error(f"設定ファイル読み込みエラー: {e}")
                    _config_cache = {}
            else:
                _config_cache = {}

            # .env または Railway 環境変数から読み込み (常に環境変数を最優先)
            env_api_key = os.environ.get("PADLET_API_KEY", "")
            env_token = os.environ.get("PADLET_SESSION_TOKEN") or os.environ.get("PADLET_TOKEN", "")
            env_sec_token = os.environ.get("PADLET_SECONDARY_TOKEN", "")
            env_webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
            
            _config_cache["padlet_api_key"] = env_api_key or _config_cache.get("padlet_api_key", "")
            _config_cache["padlet_session_token"] = env_token or _config_cache.get("padlet_session_token", "")
            _config_cache["padlet_secondary_token"] = env_sec_token or _config_cache.get("padlet_secondary_token", "")
            _config_cache["discord_webhook_url"] = env_webhook or _config_cache.get("discord_webhook_url", "")

            _config_cache.setdefault("main_board", {})
            _config_cache.setdefault("backup_board", {})
            _config_cache.setdefault("notification_section_id", None)
            _config_cache.setdefault("notification_section_title", "")
            _config_cache.setdefault("custom_key", "")
            _config_cache.setdefault("custom_value", "")
            _config_cache.setdefault("allowed_admins", [])
            _config_cache.setdefault("allowed_moderators", [])
            _config_cache.setdefault("exempt_admins", [])
            _config_cache.setdefault("max_sections", 10)
            _config_cache.setdefault("poll_interval_seconds", 1.0)
            _config_cache.setdefault("default_visitor_role", "commenter")  # 初期値: コメンター (commenter)
            _config_cache.setdefault("ddos_threshold", 5)  # 短時間投稿急増時のDDoS検知閾値
            _config_cache.setdefault("delete_non_collaborators", True)
            _config_cache.setdefault("block_duplicates", True)
            _config_cache.setdefault("block_image_repeat", True)
            _config_cache.setdefault("block_urls", False)
            _config_cache.setdefault("max_post_length", 500)
            _config_cache.setdefault("random_str_pattern", r"^.+_[a-zA-Z0-9]{4,}$")
        return _config_cache


def save_config():
    with _config_lock:
        if _config_cache is None:
            return
        tmp_file = CONFIG_FILE + ".tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(_config_cache, f, ensure_ascii=False, indent=2)
            if os.path.exists(CONFIG_FILE):
                os.replace(tmp_file, CONFIG_FILE)
            else:
                os.rename(tmp_file, CONFIG_FILE)
        except Exception as e:
            logger.error(f"設定ファイル保存失敗: {e}")


def add_log(level: str, title: str, details: str = ""):
    with _logs_lock:
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "title": title,
            "details": details
        }
        _bot_logs.insert(0, entry)
        if len(_bot_logs) > 200:
            _bot_logs.pop()


# 監視エンジン初期化
engine = SpamMonitorEngine(load_config, add_log, save_config)


def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return jsonify({"error": "ログインが必要です", "status": 401}), 401
        return f(*args, **kwargs)
    return decorated


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json or {}
    password = str(data.get("password", ""))
    target_pwd = get_admin_password()
    if secrets.compare_digest(password, target_pwd):
        session["logged_in"] = True
        return jsonify({"success": True})
    return jsonify({"error": "パスワードが正しくありません"}), 403


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("logged_in", None)
    return jsonify({"success": True})


@app.route("/api/status", methods=["GET"])
def api_status():
    cfg = load_config()
    with _logs_lock:
        logs_copy = list(_bot_logs)
    return jsonify({
        "logged_in": bool(session.get("logged_in")),
        "bot_running": engine.running,
        "is_restoring": engine.is_restoring,
        "password_is_env": bool(os.environ.get("ADMIN_PASSWORD")),
        "config": cfg,
        "logs": logs_copy
    })


@app.route("/api/config", methods=["POST"])
@auth_required
def api_save_config():
    data = request.json or {}
    cfg = load_config()

    # 更新可能なフィールド
    for key in [
        "padlet_api_key", "padlet_session_token", "padlet_secondary_token", "padlet_token", "discord_webhook_url",
        "notification_section_id", "notification_section_title", "custom_key", "custom_value",
        "allowed_admins", "allowed_moderators", "exempt_admins",
        "max_sections", "poll_interval_seconds", "default_visitor_role", "ddos_threshold",
        "delete_non_collaborators", "block_duplicates", "block_image_repeat", "block_urls", "max_post_length", "random_str_pattern"
    ]:
        if key in data:
            cfg[key] = data[key]

    save_config()
    add_log("CONFIG", "設定が更新されました")
    return jsonify({"success": True, "config": cfg})


@app.route("/api/resolve_board", methods=["POST"])
@auth_required
def api_resolve_board():
    """
    URLからボード情報を自動取得して設定に反映。
    セクション一覧および既存投稿からの投稿フィールド(custom_properties)の自動検出も行う。
    """
    data = request.json or {}
    board_type = data.get("type", "main")  # 'main' or 'backup'
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "URLを入力してください"}), 400

    cfg = load_config()
    session_token = cfg.get("padlet_session_token", "") or cfg.get("padlet_token", "")
    sec_token = cfg.get("padlet_secondary_token", "")
    client = PadletClient(session_token=session_token, secondary_session_token=sec_token)

    resolved = client.resolve_board(url)
    if not resolved:
        return jsonify({"error": "ボード情報の解析に失敗しました。URLまたはアクセス権限をご確認ください。"}), 400

    resolved["original_url"] = url
    wall_id = resolved.get("wall_id")
    wall_hashid = resolved.get("wall_hashid")

    # セクション一覧の取得
    sections = []
    if wall_id:
        sections = client.get_sections(wall_id)
        resolved["sections"] = sections

    detected_key, detected_val = None, None
    if board_type == "main":
        cfg["main_board"] = resolved
        
        # 既存投稿から投稿フィールド(custom_properties)を自動検出
        if wall_hashid:
            detected_key, detected_val = client.detect_custom_properties(wall_hashid)
            if detected_key:
                cfg["custom_key"] = detected_key
                cfg["custom_value"] = detected_val or ""
                add_log("FIELD", f"投稿フィールド(custom_properties)を自動検出: {detected_key} = {detected_val}")

        # 通知先セクションの自動選定 (未設定の場合)
        if not cfg.get("notification_section_id") and sections:
            notify_sec = next((s for s in sections if "通知" in s.get("title", "") or "ログ" in s.get("title", "")), None)
            if notify_sec:
                cfg["notification_section_id"] = notify_sec["id"]
                cfg["notification_section_title"] = notify_sec["title"]
            else:
                cfg["notification_section_id"] = sections[0]["id"]
                cfg["notification_section_title"] = sections[0]["title"]
    else:
        cfg["backup_board"] = resolved

    save_config()
    add_log("BOARD", f"{'メイン' if board_type == 'main' else 'バックアップ'}ボードを設定しました: {resolved.get('title')}")
    return jsonify({
        "success": True,
        "resolved": resolved,
        "sections": sections,
        "custom_key": detected_key or cfg.get("custom_key", ""),
        "custom_value": detected_val or cfg.get("custom_value", ""),
        "config": cfg
    })


@app.route("/api/sections", methods=["GET"])
@auth_required
def api_get_sections():
    """メインボードのセクション一覧を取得"""
    cfg = load_config()
    main_board = cfg.get("main_board", {})
    wall_id = main_board.get("wall_id")
    if not wall_id:
        return jsonify({"sections": []})

    session_token = cfg.get("padlet_session_token", "") or cfg.get("padlet_token", "")
    sec_token = cfg.get("padlet_secondary_token", "")
    client = PadletClient(session_token=session_token, secondary_session_token=sec_token)
    sections = client.get_sections(wall_id)
    return jsonify({"sections": sections})


@app.route("/api/sections/create_notify", methods=["POST"])
@auth_required
def api_create_notify_section():
    """メインボード上に通知用セクション『🔔防衛Bot通知』を新規作成"""
    cfg = load_config()
    main_board = cfg.get("main_board", {})
    wall_id = main_board.get("wall_id")
    if not wall_id:
        return jsonify({"error": "メインボードが設定されていません"}), 400

    data = request.json or {}
    sec_title = data.get("title", "🔔防衛Bot通知").strip() or "🔔防衛Bot通知"

    session_token = cfg.get("padlet_session_token", "") or cfg.get("padlet_token", "")
    sec_token = cfg.get("padlet_secondary_token", "")
    client = PadletClient(session_token=session_token, secondary_session_token=sec_token)

    created = client.create_section(wall_id, sec_title)
    if not created:
        return jsonify({"error": "通知セクションの作成に失敗しました"}), 500

    cfg["notification_section_id"] = created["id"]
    cfg["notification_section_title"] = sec_title
    save_config()
    add_log("SECTION", f"通知用セクションを作成しました: {sec_title} (ID: {created['id']})")

    sections = client.get_sections(wall_id)
    return jsonify({"success": True, "created": created, "sections": sections, "config": cfg})


@app.route("/api/bot/toggle", methods=["POST"])
@auth_required
def api_toggle_bot():
    """Botの起動 / 停止切り替え"""
    data = request.json or {}
    start = data.get("start", True)
    if start:
        engine.start()
    else:
        engine.stop()
    return jsonify({"success": True, "bot_running": engine.running})


@app.route("/api/bot/force_recovery", methods=["POST"])
@auth_required
def api_force_recovery():
    """手動でのバックアップ復元実行"""
    cfg = load_config()
    main_wall_id = cfg.get("main_board", {}).get("wall_id")
    if not main_wall_id:
        return jsonify({"error": "メインボードが設定されていません"}), 400

    session_token = cfg.get("padlet_session_token", "") or cfg.get("padlet_token", "")
    sec_token = cfg.get("padlet_secondary_token", "")
    if not session_token:
        return jsonify({"error": "Padlet Session Token が設定されていません"}), 400

    client = PadletClient(session_token=session_token, secondary_session_token=sec_token)
    main_board = cfg.get("main_board", {})
    main_wall_hashid = main_board.get("wall_hashid", "")
    owner_id = main_board.get("owner_id")

    sections = client.get_sections(main_wall_id)
    collabs = client.get_collaborators(main_wall_hashid, main_wall_id)
    collab_map = {c["user_id"]: c for c in collabs}

    add_log("MANUAL", "手動復旧がリクエストされました")
    engine.execute_section_recovery(main_wall_id, main_wall_hashid, sections, collab_map, owner_id)
    return jsonify({"success": True, "message": "バックアップ復元シーケンスを開始しました"})


@app.route("/api/bot/sync_posts", methods=["POST"])
@auth_required
def api_sync_posts():
    """手動でのバックアップ投稿コピー・同期実行"""
    add_log("MANUAL", "バックアップからメインへの投稿同期がリクエストされました")
    res = engine.sync_backup_posts_to_main()
    if not res.get("success"):
        return jsonify(res), 400
    return jsonify(res)


@app.route("/api/collaborators", methods=["GET"])
@auth_required
def api_get_collaborators():
    """現在のメインボードのコラボレーター一覧を取得"""
    cfg = load_config()
    main_board = cfg.get("main_board", {})
    wall_id = main_board.get("wall_id")
    wall_hashid = main_board.get("wall_hashid")
    session_token = cfg.get("padlet_session_token", "") or cfg.get("padlet_token", "")
    sec_token = cfg.get("padlet_secondary_token", "")

    if not wall_hashid:
        return jsonify({"collaborators": []})

    client = PadletClient(session_token=session_token, secondary_session_token=sec_token)
    collabs = client.get_collaborators(wall_hashid, wall_id)
    return jsonify({"collaborators": collabs})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    current_pwd, is_env = _get_or_create_admin_password()
    
    print("\n" + "=" * 65)
    print("🛡️  Padlet 荒らし対策Bot & 自動復旧システム (Ultra 2.0)")
    print(f"🌐 起動ポート: {port}")
    if is_env:
        print("🔑 管理者パスワード: [環境変数 ADMIN_PASSWORD により安全に設定済]")
    else:
        print("⚠️  [SECURITY NOTICE] 環境変数 ADMIN_PASSWORD が未設定です。")
        print(f"🔑 自動生成された安全な管理者パスワード: {current_pwd}")
    print("💡 監視エンジンは安全のため初期状態で【停止中】です。")
    print("👉 管理画面でURL・権限設定を確認後、【監視開始】ボタンを押して起動してください。")
    print("=" * 65 + "\n")

    # 最初は監視オフ（管理画面から手動でオンにする）
    app.run(host="0.0.0.0", port=port, debug=False)
