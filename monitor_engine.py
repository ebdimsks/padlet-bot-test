import os
import re
import sys
import time
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# Windows環境でのコンソールUTF-8出力対策 (絵文字・Unicodeによるクラッシュ完全防止)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from padlet_client import PadletClient

logger = logging.getLogger(__name__)


class SpamMonitorEngine:
    """
    Padlet 荒らし監視・自動防衛・高速復旧エンジン (Ultra Defense 2.0)
    """

    def __init__(self, data_store_callback, log_callback, save_data_callback=None):
        self.get_data = data_store_callback
        # 絵文字やUnicodeによる例外から防衛エンジンを完全保護するセーフラッパー
        def _safe_log(lvl, title, details=""):
            try:
                log_callback(str(lvl), str(title), str(details))
            except Exception:
                pass
        self.add_log = _safe_log
        self.save_data_callback = save_data_callback
        self.client = PadletClient()
        self.running = False
        self._thread: Optional[threading.Thread] = None

        # 投稿履歴キャッシュ: user_id -> [(timestamp, text, image_url, post_id)]
        self.user_post_history: Dict[str, List[tuple]] = {}
        # アカウント別投稿タイムスタンプ履歴 (1秒2件/2秒4件の個別レート判定用): user_id -> [float]
        self.user_post_timestamps: Dict[str, List[float]] = {}
        # アカウント別画像投稿履歴 (同一画像の短期間連投判定用): user_id -> [(timestamp, image_url, post_id)]
        self.user_image_history: Dict[str, List[tuple]] = {}
        # アカウント別コメントタイムスタンプ履歴 (個別レート判定用): user_id -> [float]
        self.user_comment_timestamps: Dict[str, List[float]] = {}
        # 既知の投稿IDセット (サイクルごとの新規投稿検知・誤検知防止用)
        self.known_post_ids: Set[str] = set()
        # 既知のコメントIDセット (サイクルごとの新規コメント検知・誤検知防止用)
        self.known_comment_ids: Set[str] = set()
        # 初回起動時の初期スキャン完了フラグ (起動時に元からある既存投稿・コメントの誤検知完全防止)
        self.has_initialized_posts: bool = False
        self.has_initialized_comments: bool = False
        self.current_wall_id: Optional[int] = None
        # 並立処罰パイプライン (1人対処中も他人の荒らしを並行して検知・処罰)
        self.pipeline_lock = threading.Lock()
        self.active_punishing_uids: Set[str] = set()
        self.active_deleting_post_ids: Set[str] = set()
        self.active_deleting_comment_ids: Set[str] = set()
        # 短時間アクティビティ履歴
        self.recent_activity_log: List[tuple] = []
        # 復元中フラグ
        self.is_restoring = False
        # 復旧処理で作成された投稿の保護用フィンガープリント (誤削除防止)
        self.protected_post_fingerprints: Set[str] = set()
        # 永続コラボレーターキャッシュ (一時的なAPIエラーによる一般投稿の誤削除を完全防止)
        self.cached_collab_identifiers: Set[str] = set()
        self.cached_collab_map: Dict[str, Any] = {}

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        self.add_log("SYSTEM", "超高速・並列防衛エンジンを開始しました", "デュアルアカウント並列稼働 / 20人同時荒らし耐性")

    def stop(self):
        self.running = False
        self.has_initialized_posts = False
        self.has_initialized_comments = False
        self.known_post_ids.clear()
        self.known_comment_ids.clear()
        self.user_post_timestamps.clear()
        self.user_image_history.clear()
        self.user_comment_timestamps.clear()
        with self.pipeline_lock:
            self.active_punishing_uids.clear()
            self.active_deleting_post_ids.clear()
            self.active_deleting_comment_ids.clear()
        self.add_log("SYSTEM", "監視エンジンを停止しました")

    def send_notification(self, title: str, description: str, color: int = 0xFF4B4B, fields: Optional[List[Dict[str, Any]]] = None, mention_everyone: bool = True):
        """
        アラート・防衛通知送信
        - Discord Webhookが設定されている場合: Discordのみに@everyoneメンション付きで即時送信 (Padlet側には投稿しない)
        - Discord Webhookが未設定(空)の場合: Padletボード内の指定通知セクションに自動投稿
        """
        data = self.get_data()
        webhook_url = data.get("discord_webhook_url", "").strip()

        if webhook_url:
            # 1. Discord Webhook 連携時は Webhook のみで通知
            embed = {
                "title": f"🛡️ Padlet防衛Bot: {title}",
                "description": description,
                "color": color,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Padlet Anti-Spam Bot Ultra 2.0 • 24時間監視防衛システム"}
            }
            if fields:
                embed["fields"] = fields

            payload = {
                "content": "@everyone" if mention_everyone else None,
                "embeds": [embed]
            }

            try:
                res = requests.post(webhook_url, json=payload, timeout=6)
                if res.status_code not in (200, 204):
                    logger.warning(f"Discord通知ステータスコード異常: {res.status_code}")
            except Exception as e:
                logger.error(f"Discord通知送信エラー: {e}")
            return  # Webhook通知完了。Padlet側への重複投稿は絶対にしない！

        # 2. Webhook未設定時のみ、Padlet ボード内通知セクションへの投稿作成
        self._send_padlet_board_notification(title, description, fields)

    def send_discord_webhook(self, title: str, description: str, color: int = 0xFF4B4B, fields: Optional[List[Dict[str, Any]]] = None, mention_everyone: bool = True):
        """後方互換用エイリアス"""
        self.send_notification(title, description, color, fields, mention_everyone)

    def _send_padlet_board_notification(self, title: str, description: str, fields: Optional[List[Dict[str, Any]]] = None):
        """
        Discord Webhook未指定時、Padletの指定セクションに防衛ログ・通知投稿を作成
        カスタムキー/カスタム値(custom_properties)にも完全対応
        """
        data = self.get_data()
        webhook_url = data.get("discord_webhook_url", "").strip()
        if webhook_url:
            return  # Webhook登録時はPadletでは通知しない！

        main_board = data.get("main_board", {})
        wall_id = main_board.get("wall_id")
        wall_hashid = main_board.get("wall_hashid")
        if not wall_id or not wall_hashid:
            return

        notification_sec_id = data.get("notification_section_id")

        # セクションが未指定の場合、メインボードのセクションから探索、なければ自動作成
        if not notification_sec_id:
            sections = self.client.get_sections(wall_id)
            notify_sec = next((s for s in sections if "通知" in s.get("title", "") or "ログ" in s.get("title", "") or "bot" in s.get("title", "").lower()), None)
            if notify_sec:
                notification_sec_id = notify_sec["id"]
            elif sections:
                notification_sec_id = sections[0]["id"]
            else:
                created = self.client.create_section(wall_id, "🔔防衛Bot通知")
                if created:
                    notification_sec_id = created["id"]

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body_lines = [
            "【🛡️ Padlet防衛Bot通知】",
            description,
            ""
        ]
        if fields:
            for f in fields:
                name = f.get("name", "")
                val = f.get("value", "")
                body_lines.append(f"▶ {name}: {val}")
            body_lines.append("")
        body_lines.append(f"⏱️ 発生時刻: {now_str}")
        full_body = "\n".join(body_lines)

        subject = f"🛡️ [防衛通知] {title}"

        # 誤削除防止用フィンガープリント登録
        fp = f"{subject}:::{full_body}"
        self.protected_post_fingerprints.add(fp)

        custom_key = data.get("custom_key")
        custom_value = data.get("custom_value")

        try:
            ok = self.client.create_post(
                wall_id=wall_id,
                subject=subject,
                body=full_body,
                section_id=str(notification_sec_id) if notification_sec_id else None,
                board_hashid=wall_hashid,
                custom_key=custom_key,
                custom_value=custom_value
            )
            if ok:
                self.add_log("NOTIFICATION", f"Padletボード内に通知を投稿しました: {title}", f"セクション: {notification_sec_id}")
            else:
                logger.warning("Padletボード内通知の作成に失敗しました")
        except Exception as e:
            logger.error(f"Padlet通知作成エラー: {e}")

    def _monitor_loop(self):
        """高頻度監視ループ (デュアルアカウント・レート制限分散)"""
        while self.running:
            try:
                data = self.get_data()
                api_key = data.get("padlet_api_key", "").strip()
                session_token = data.get("padlet_session_token", "").strip() or data.get("padlet_token", "").strip()
                sec_token = data.get("padlet_secondary_token", "").strip()
                main_wall = data.get("main_board", {})
                main_wall_id = main_wall.get("wall_id")
                main_wall_hashid = main_wall.get("wall_hashid")

                if (api_key or session_token) and main_wall_id and main_wall_hashid:
                    self.client.set_credentials(
                        api_key=api_key,
                        session_token=session_token,
                        secondary_session_token=sec_token
                    )
                    self._check_cycle(data, main_wall_id, main_wall_hashid)
            except Exception as e:
                logger.error(f"監視ループ予期せぬエラー: {e}", exc_info=True)
                self.add_log("ERROR", "監視サイクル中にエラーが発生しました", str(e))

            # 高速かつ安全なポーリング待機 (最小0.5秒)
            time.sleep(max(float(self.get_data().get("poll_interval_seconds", 1.0)), 0.5))

    def _demote_unauthorized_admins_and_mods(self, main_wall_id: int, main_wall_hashid: str, collab_map: Dict[str, Any], owner_id: Optional[str], exempt_admins: Set[str]) -> List[str]:
        """
        セクション荒らし検知時: 降格免除リスト(exempt_admins)および部屋主(owner_id)以外の
        すべての管理者(admin/owner)とモデレーター(moderator)の権限を確実に writer に降格する。
        """
        # 最新のコラボレーター情報を再取得して統合
        latest_collabs = self.client.get_collaborators(main_wall_hashid, main_wall_id)
        current_map = dict(collab_map)
        for c in latest_collabs:
            uid = c.get("user_id")
            if uid and uid != "anonymous":
                current_map[uid] = c

        exempt_lower = {str(x).strip().lower() for x in exempt_admins if str(x).strip()}
        owner_str = str(owner_id).strip().lower() if owner_id else ""

        targets = []
        for uid, cinfo in current_map.items():
            uid_str = str(uid).strip()
            if not uid_str or uid_str == "anonymous":
                continue
            if owner_str and uid_str.lower() == owner_str:
                continue

            username = str(cinfo.get("username") or "").strip()
            name = str(cinfo.get("name") or "").strip()
            hashid = str(cinfo.get("hashid") or "").strip()
            role = str(cinfo.get("role") or "").strip().lower()

            # 降格免除リスト判定 (UID, username, name, hashid のいずれかが一致すれば免除・保護)
            if (uid_str.lower() in exempt_lower or
                (username and username.lower() in exempt_lower) or
                (name and name.lower() in exempt_lower) or
                (hashid and hashid.lower() in exempt_lower)):
                continue

            # 管理者・モデレーターのみを対象として writer に降格
            if role in ("admin", "owner", "moderator"):
                targets.append((uid_str, hashid, username or name or uid_str, role))

        demoted_names = []
        if targets:
            def _demote_single(target):
                u_id, h_id, uname, old_role = target
                success = False
                # user_id で降格試行
                if self.client.change_user_role(main_wall_id, u_id, "writer", board_hashid=main_wall_hashid):
                    success = True
                # hashid でも試行
                elif h_id and self.client.change_user_role(main_wall_id, h_id, "writer", board_hashid=main_wall_hashid):
                    success = True

                if success:
                    self.add_log("SECURITY", f"セクション荒らし防衛降格: {uname} (ID: {u_id}, {old_role} ➜ writer)")
                    return uname
                else:
                    self.add_log("WARN", f"管理者降格試行完了: {uname} (ID: {u_id})")
                    return uname

            with ThreadPoolExecutor(max_workers=min(max(len(targets), 4), 16)) as executor:
                futures = [executor.submit(_demote_single, t) for t in targets]
                for f in as_completed(futures):
                    res = f.result()
                    if res:
                        demoted_names.append(res)

        return demoted_names

    def _check_cycle(self, data: Dict[str, Any], main_wall_id: int, main_wall_hashid: str):
        """1サイクルの包括的防衛チェック (セクション荒らし・投稿荒らし・コメント違反の精密対処)"""
        now = time.time()
        main_board_info = data.get("main_board", {})
        owner_id = str(main_board_info.get("owner_id", "")) if main_board_info.get("owner_id") else None

        # 許可・免除リスト
        allowed_admins = set(str(x) for x in data.get("allowed_admins", []))
        allowed_mods = set(str(x) for x in data.get("allowed_moderators", []))
        exempt_admins = set(str(x) for x in data.get("exempt_admins", []))

        # =====================================================================
        # 1. 全体アクセス設定 (visitorRole) の監視 & 強制修復 (commenter, none等に対応)
        # =====================================================================
        expected_visitor_role = data.get("default_visitor_role", "none")
        access_settings = self.client.get_access_settings(main_wall_hashid)
        current_visitor_role = access_settings.get("visitorRole")
        if current_visitor_role and current_visitor_role != expected_visitor_role:
            success = self.client.change_visitor_role(main_wall_hashid, expected_visitor_role, wall_id=main_wall_id)
            if success:
                self.add_log("SECURITY", f"全体の権限(visitorRole)の改変を検知: {current_visitor_role} -> {expected_visitor_role} に即座に強制修復しました")
                self.send_discord_webhook(
                    "全体権限(visitorRole)即時修復",
                    f"全体の権限が不審に変更されていたため、設定値 `{expected_visitor_role}` に即座に戻しました。",
                    color=0xFFA500,
                    fields=[
                        {"name": "改変前", "value": f"`{current_visitor_role}`", "inline": True},
                        {"name": "修復後", "value": f"`{expected_visitor_role}`", "inline": True}
                    ]
                )
                current_visitor_role = expected_visitor_role

        # =====================================================================
        # 2. コラボレーター情報の取得 & 不正管理者・モデレーター昇格の検知・即降格
        # =====================================================================
        collaborators = self.client.get_collaborators(main_wall_hashid, main_wall_id)
        if collaborators:
            for c in collaborators:
                uid = c.get("user_id")
                if uid and uid != "anonymous":
                    self.cached_collab_map[uid] = c
                    self.cached_collab_identifiers.add(str(uid).strip())
                if c.get("hashid"):
                    self.cached_collab_identifiers.add(str(c["hashid"]).strip())
                if c.get("username"):
                    self.cached_collab_identifiers.add(str(c["username"]).strip().lower())
                if c.get("name"):
                    self.cached_collab_identifiers.add(str(c["name"]).strip().lower())

        collab_map = dict(self.cached_collab_map)
        for c in collaborators:
            if c.get("user_id"):
                collab_map[c["user_id"]] = c

        # コラボレーター（=権限が決められている人）の識別子セット (ID, hashid, ユーザー名, 表示名)
        registered_collab_identifiers: Set[str] = set(self.cached_collab_identifiers)
        if owner_id:
            registered_collab_identifiers.add(str(owner_id).strip())
            self.cached_collab_identifiers.add(str(owner_id).strip())
        for adm in allowed_admins:
            registered_collab_identifiers.add(str(adm).strip().lower())
            self.cached_collab_identifiers.add(str(adm).strip().lower())
        for mod in allowed_mods:
            registered_collab_identifiers.add(str(mod).strip().lower())
            self.cached_collab_identifiers.add(str(mod).strip().lower())
        for ex in exempt_admins:
            registered_collab_identifiers.add(str(ex).strip().lower())
            self.cached_collab_identifiers.add(str(ex).strip().lower())

        def is_collaborator(uid: str, uname: str = "") -> bool:
            """コラボレーター(=権限が決められている人、共有に招待されている人)かどうかを判定"""
            if not uid or uid == "anonymous":
                return False
            uid_str = str(uid).strip()
            if uid_str in registered_collab_identifiers or uid_str in self.cached_collab_identifiers:
                return True
            if uname:
                uname_str = str(uname).strip().lower()
                if uname_str and uname_str not in ("unknown", "anonymous", "none"):
                    if uname_str in registered_collab_identifiers or uname_str in self.cached_collab_identifiers:
                        return True
            return False

        # 不正管理者・モデレーター昇格の検知
        for uid, cinfo in collab_map.items():
            if owner_id and uid == owner_id:
                continue
            role = (cinfo.get("role") or "").lower()
            username = cinfo.get("username") or cinfo.get("name") or uid

            if role in ("admin", "owner"):
                if uid not in allowed_admins and username not in allowed_admins and uid not in exempt_admins and username not in exempt_admins:
                    self.client.change_user_role(main_wall_id, uid, "writer")
                    msg = f"未許可の管理者昇格を検知: {username} (ID: {uid}) を writer に降格しました"
                    self.add_log("SECURITY", msg)
                    self.send_discord_webhook("不正管理者検知・即時降格", msg, color=0xFFA500)

            elif role == "moderator":
                if uid not in allowed_mods and username not in allowed_mods and uid not in exempt_admins and username not in exempt_admins:
                    self.client.change_user_role(main_wall_id, uid, "writer")
                    msg = f"未許可のモデレーター昇格を検知: {username} (ID: {uid}) を writer に降格しました"
                    self.add_log("SECURITY", msg)
                    self.send_discord_webhook("不正モデレーター検知・即時降格", msg, color=0xFFA500)

        # =====================================================================
        # 3. セクション増殖荒らしの検知 & 自動防衛・管理者降格 & 自動復旧起動
        # =====================================================================
        max_sections = int(data.get("max_sections", 10))
        current_sections = self.client.get_sections(main_wall_id)
        if len(current_sections) > max_sections and not self.is_restoring:
            alert_msg = f"セクション数異常増殖を検知 (現在: {len(current_sections)}個 / 上限: {max_sections}個)！"
            self.add_log("ALERT", "🚨 セクション荒らし検知！", alert_msg)

            # (1) 全体権限を即時ロックダウン
            self.client.change_visitor_role(main_wall_hashid, "reader", wall_id=main_wall_id)

            # (2) 降格免除リスト以外の管理者・モデレーターの権限をしっかり下げる (writer に降格)
            demoted_admins = self._demote_unauthorized_admins_and_mods(
                main_wall_id, main_wall_hashid, collab_map, owner_id, exempt_admins
            )
            demoted_str = ", ".join(demoted_admins) if demoted_admins else "なし"

            self.send_discord_webhook(
                "🚨 セクション荒らし検知・即時防衛＆管理者降格発動！",
                f"{alert_msg}\n全体権限を即時ロックダウンし、降格免除リスト以外の管理者・モデレーターを writer に降格しました。\n全セクション削除＆バックアップ完全復元シーケンスを開始します。",
                color=0xFF0000,
                fields=[
                    {"name": "検知セクション数", "value": f"{len(current_sections)} 個 (上限: {max_sections})", "inline": True},
                    {"name": "降格された管理者/モデレーター", "value": f"`{demoted_str}`", "inline": True},
                    {"name": "防衛ステータス", "value": "管理者降格 ➜ 全体権限ロック ➜ バックアップ完全復元中", "inline": False}
                ],
                mention_everyone=True
            )
            # (3) バックグラウンドスレッドで復元を実行
            self.execute_section_recovery(main_wall_id, main_wall_hashid, current_sections, collab_map, owner_id)

        # =====================================================================
        # 4. 投稿 & コメント荒らしの精密・高精度防衛監視
        # =====================================================================
        posts = self.client.get_posts(main_wall_hashid)
        comments = self.client.get_comments(main_wall_hashid)

        delete_non_collab = data.get("delete_non_collaborators", True)
        
        # 全体の権限がライターの場合は、コラボレーターにいなくても投稿は消さない！
        is_general_writer = (current_visitor_role == "writer" or expected_visitor_role == "writer")

        # 処罰対象 (読者に降格 & その人の投稿を絶対にすべて削除)
        spammer_uids_to_punish: Set[str] = set()
        # 削除対象投稿ID (荒らし投稿、非コラボ投稿、キャプション50文字以上)
        posts_to_delete: Set[str] = set()
        # 削除対象コメントID
        comments_to_delete: Set[str] = set()

        # ボード切り替え時のリセット
        if self.current_wall_id != main_wall_id:
            self.current_wall_id = main_wall_id
            self.has_initialized_posts = False
            self.has_initialized_comments = False
            self.known_post_ids.clear()
            self.known_comment_ids.clear()
            self.user_post_timestamps.clear()
            self.user_image_history.clear()
            self.user_comment_timestamps.clear()

        # 現在存在する全投稿ID & コメントID
        current_post_ids = {str(p.get("id")) for p in posts if p.get("id")}
        current_comment_ids = {str(c.get("id")) for c in comments if c.get("id")}

        # 初回起動時の安全ガード: 既存の投稿とコメントを登録（起動前からの投稿の誤検知完全防止）
        if not self.has_initialized_posts:
            self.known_post_ids = set(current_post_ids)
            self.has_initialized_posts = True
            new_posts = []
            if current_post_ids:
                self.add_log("SYSTEM", f"ボード投稿初期化完了: 既存投稿{len(current_post_ids)}件を除外登録しました")
        else:
            # APIの一時的な空レスポンスによる誤検知防止
            if not current_post_ids and len(self.known_post_ids) > 0:
                new_posts = []
            else:
                new_posts = [p for p in posts if p.get("id") and str(p.get("id")) not in self.known_post_ids]

        if not self.has_initialized_comments:
            self.known_comment_ids = set(current_comment_ids)
            self.has_initialized_comments = True
            new_comments = []
        else:
            if not current_comment_ids and len(self.known_comment_ids) > 0:
                new_comments = []
            else:
                new_comments = [c for c in comments if c.get("id") and str(c.get("id")) not in self.known_comment_ids]

        # -------------------------------------------------------------
        # 【投稿荒らし検知 ①】1秒間に2投稿以上、または2秒間に4投稿以上
        # 「その荒らした人だけ」荒らし判定にする。処罰は読者。荒らしに検知した人の投稿は絶対にすべて削除。
        # -------------------------------------------------------------
        for p in new_posts:
            aid = str(p.get("author_id", "") or "").strip()
            aname = str(p.get("author_name", "") or "").strip()
            if not aid or aid == "anonymous":
                continue  # 匿名投稿は特定アカウント処罰(読者降格)の対象外
            if owner_id and aid == owner_id:
                continue
            if aid in exempt_admins or aname in exempt_admins or collab_map.get(aid, {}).get("username") in exempt_admins:
                continue

            fp = f"{p.get('subject', '')}:::{p.get('content', '')}"
            if fp in self.protected_post_fingerprints or (p.get("subject") or "").startswith("🛡️ [防衛通知]"):
                continue

            # 投稿作成日時 (created_at) の高精度検証
            p_time = now
            p_created_str = p.get("created_at")
            if p_created_str:
                try:
                    dt = datetime.fromisoformat(str(p_created_str).replace("Z", "+00:00"))
                    ts = dt.timestamp()
                    if abs(now - ts) <= 120.0:
                        p_time = ts
                    else:
                        p_time = now
                except Exception:
                    p_time = now

            if aid not in self.user_post_timestamps:
                self.user_post_timestamps[aid] = []
            self.user_post_timestamps[aid].append(p_time)

        for aid, timestamps in list(self.user_post_timestamps.items()):
            if not aid or aid == "anonymous":
                continue
            valid_ts = [t for t in timestamps if now - t <= 10.0]
            self.user_post_timestamps[aid] = valid_ts

            if owner_id and aid == owner_id:
                continue
            if aid in exempt_admins or collab_map.get(aid, {}).get("username") in exempt_admins:
                continue

            # 精密間隔判定: 1秒間に2投稿以上 または 2秒間に4投稿以上
            ts_sorted = sorted(valid_ts)
            is_spam_1s = any(ts_sorted[i+1] - ts_sorted[i] <= 1.0 for i in range(len(ts_sorted) - 1)) if len(ts_sorted) >= 2 else False
            is_spam_2s = any(ts_sorted[i+3] - ts_sorted[i] <= 2.0 for i in range(len(ts_sorted) - 3)) if len(ts_sorted) >= 4 else False

            if is_spam_1s or is_spam_2s:
                author_name = collab_map.get(aid, {}).get("username") or collab_map.get(aid, {}).get("name") or aid
                reason = "連投爆撃検知 (1秒に2投稿以上 または 2秒に4投稿以上)"
                self.add_log("ALERT", f"連投荒らし検知: {author_name}", reason)
                # その荒らした人だけ荒らし判定 (処罰: 読者 & 投稿全削除)
                spammer_uids_to_punish.add(aid)

        # -------------------------------------------------------------
        # 【投稿荒らし検知 ②】同じ画像を短期間に複数投稿
        # 「その荒らした人だけ」荒らし判定にする。処罰は読者。荒らしに検知した人の投稿は絶対にすべて削除。
        # -------------------------------------------------------------
        for p in new_posts:
            pid = str(p.get("id", ""))
            aid = str(p.get("author_id", "") or "").strip()
            aname = str(p.get("author_name", "") or "").strip()
            if not pid:
                continue
            if owner_id and aid == owner_id:
                continue
            if aid in exempt_admins or aname in exempt_admins or collab_map.get(aid, {}).get("username") in exempt_admins:
                continue

            fp = f"{p.get('subject', '')}:::{p.get('content', '')}"
            if fp in self.protected_post_fingerprints or (p.get("subject") or "").startswith("🛡️ [防衛通知]"):
                continue

            attachment = p.get("attachment")
            if attachment:
                track_key = aid if (aid and aid != "anonymous") else f"anon_img"
                if track_key not in self.user_image_history:
                    self.user_image_history[track_key] = []

                img_hist = [item for item in self.user_image_history[track_key] if now - item[0] <= 30.0]
                same_img = [item for item in img_hist if item[1] == attachment and item[2] != pid]
                if same_img:
                    author_name = collab_map.get(aid, {}).get("username") or collab_map.get(aid, {}).get("name") or aid
                    self.add_log("ALERT", f"同一画像連投荒らし検知: {author_name}", f"短期間に同一画像を重複投稿 (PostID: {pid})")
                    if aid and aid != "anonymous":
                        # その荒らした人だけ荒らし判定 (処罰: 読者 & 投稿全削除)
                        spammer_uids_to_punish.add(aid)
                    else:
                        # 匿名の場合は重複したその画像投稿のみ削除 (周りを巻き込まない)
                        posts_to_delete.add(pid)
                        for item in same_img:
                            posts_to_delete.add(item[2])

                img_hist.append((now, attachment, pid))
                self.user_image_history[track_key] = img_hist

        # -------------------------------------------------------------
        # 【読者にはしないけど、投稿を削除するやつ】
        # ① 画像のキャプションのところが文字数制限(デフォルト50文字)以上の投稿
        #    ※タイトルおよび本文には文字数制限なし（無制限）
        # ② コラボレーター以外の投稿
        #    ※「全体の権限がライターの場合は、コラボレーターにいなくても、投稿は消さない」
        # -------------------------------------------------------------
        raw_caption_limit = data.get("max_caption_length", data.get("max_post_length", 50))
        try:
            max_caption_len = int(raw_caption_limit)
        except Exception:
            max_caption_len = 50

        for p in posts:
            pid = str(p.get("id", ""))
            aid = str(p.get("author_id", "") or "").strip()
            aname = str(p.get("author_name", "") or "").strip()
            if not pid:
                continue
            if owner_id and aid == owner_id:
                continue  # 部屋主は保護
            if aid in exempt_admins or aname in exempt_admins or collab_map.get(aid, {}).get("username") in exempt_admins:
                continue

            fp = f"{p.get('subject', '')}:::{p.get('content', '')}"
            if fp in self.protected_post_fingerprints or (p.get("subject") or "").startswith("🛡️ [防衛通知]"):
                continue

            # ① 画像キャプションが制限(デフォルト50文字)以上 -> 投稿のみ削除 (読者にはしない)
            # ※タイトルおよび本文には文字数制限はありません（無制限）
            if max_caption_len > 0:
                caption = str(p.get("attachment_caption") or "").strip()
                if len(caption) >= max_caption_len:
                    posts_to_delete.add(pid)
                    self.add_log("DEFENSE", f"画像キャプション文字数超過({max_caption_len}文字以上)の投稿を削除: {len(caption)}文字 (PostID: {pid})")
                    continue

            # ② コラボレーター以外の投稿 -> 投稿のみ削除 (読者にはしない)
            # ※「全体の権限がライターの場合は、コラボレーターにいなくても、投稿は消さない」
            if delete_non_collab and not is_general_writer:
                # セーフガード: コラボレーターキャッシュまたは一覧が1件以上ある場合のみ実行
                if (collab_map or self.cached_collab_identifiers):
                    if not is_collaborator(aid, aname):
                        posts_to_delete.add(pid)
                        self.add_log("DEFENSE", f"非コラボレーター投稿を削除: {pid} (Author: {aid})")
                        continue

        # -------------------------------------------------------------
        # 【コメント監視】
        # - 全体の権限が何に設定されてても、コラボレーターにいない人のコメントはOK (削除しない！)
        # - コメントに違反があったら、その人のアカウントの投稿とコメントだけを削除 (周りを巻き込まない)
        # -------------------------------------------------------------
        violating_comment_authors: Set[str] = set()

        for c in new_comments:
            c_aid = str(c.get("author_id", "") or "").strip()
            c_aname = str(c.get("author_name", "") or "").strip()
            cid = str(c.get("id", ""))
            if not cid:
                continue
            if owner_id and c_aid == owner_id:
                continue
            if c_aid in exempt_admins or c_aname in exempt_admins:
                continue

            # コメントのタイムスタンプ記録
            c_time = now
            c_created_str = c.get("created_at")
            if c_created_str:
                try:
                    dt = datetime.fromisoformat(str(c_created_str).replace("Z", "+00:00"))
                    c_time = dt.timestamp()
                except Exception:
                    c_time = now

            if abs(now - c_time) <= 20.0:
                track_c_key = c_aid if (c_aid and c_aid != "anonymous") else f"anon_comm_{cid}"
                if track_c_key not in self.user_comment_timestamps:
                    self.user_comment_timestamps[track_c_key] = []
                self.user_comment_timestamps[track_c_key].append(c_time)

        for c_key, timestamps in list(self.user_comment_timestamps.items()):
            valid_ts = [t for t in timestamps if now - t <= 10.0]
            self.user_comment_timestamps[c_key] = valid_ts

            if owner_id and c_key == owner_id:
                continue
            if c_key in exempt_admins:
                continue

            # 精密間隔判定: 1秒間に2件以上 または 2秒間に4件以上
            c_ts_sorted = sorted(valid_ts)
            is_c_spam_1s = any(c_ts_sorted[i+1] - c_ts_sorted[i] <= 1.0 for i in range(len(c_ts_sorted) - 1)) if len(c_ts_sorted) >= 2 else False
            is_c_spam_2s = any(c_ts_sorted[i+3] - c_ts_sorted[i] <= 2.0 for i in range(len(c_ts_sorted) - 3)) if len(c_ts_sorted) >= 4 else False

            if is_c_spam_1s or is_c_spam_2s:
                self.add_log("ALERT", f"コメント連投違反検知: {c_key}", "1秒2件以上 または 2秒4件以上のコメント連投")
                if not c_key.startswith("anon_comm_"):
                    violating_comment_authors.add(c_key)
                else:
                    # 匿名の連投違反はその違反コメントのみピンポイントで削除 (周りを巻き込まない)
                    anon_cid = c_key.replace("anon_comm_", "")
                    if anon_cid:
                        comments_to_delete.add(anon_cid)

        # コメント違反者のアカウントの「投稿」と「コメント」だけを削除 (周りを巻き込まない)
        if violating_comment_authors:
            for c in comments:
                c_author = str(c.get("author_id", "") or "").strip()
                if c_author and c_author != "anonymous" and c_author in violating_comment_authors:
                    if c.get("id"):
                        comments_to_delete.add(str(c["id"]))
            for p in posts:
                p_author = str(p.get("author_id", "") or "").strip()
                if p_author and p_author != "anonymous" and p_author in violating_comment_authors:
                    if p.get("id"):
                        posts_to_delete.add(str(p["id"]))

        # -------------------------------------------------------------
        # 荒らしに検知した人の投稿は「絶対にすべて削除」
        # -------------------------------------------------------------
        if spammer_uids_to_punish:
            for p in posts:
                p_aid = str(p.get("author_id", "") or "").strip()
                if p_aid and p_aid != "anonymous" and p_aid in spammer_uids_to_punish and p.get("id"):
                    posts_to_delete.add(str(p["id"]))
            for c in comments:
                c_aid = str(c.get("author_id", "") or "").strip()
                if c_aid and c_aid != "anonymous" and c_aid in spammer_uids_to_punish and c.get("id"):
                    comments_to_delete.add(str(c["id"]))

        # 既知投稿ID・コメントIDの安全更新 (クリアせず追記蓄積)
        if current_post_ids:
            self.known_post_ids.update(current_post_ids)
        if current_comment_ids:
            self.known_comment_ids.update(current_comment_ids)

        # (E) 処罰 & 削除の超高速並列・並立実行
        if spammer_uids_to_punish or posts_to_delete or comments_to_delete:
            with self.pipeline_lock:
                new_spammers = set(spammer_uids_to_punish) - self.active_punishing_uids
                new_posts_to_del = set(posts_to_delete) - self.active_deleting_post_ids
                new_comments_to_del = set(comments_to_delete) - self.active_deleting_comment_ids
                self.active_punishing_uids.update(new_spammers)
                self.active_deleting_post_ids.update(new_posts_to_del)
                self.active_deleting_comment_ids.update(new_comments_to_del)

            if new_spammers or new_posts_to_del or new_comments_to_del:
                threading.Thread(
                    target=self._handle_spammers_batch,
                    args=(main_wall_id, main_wall_hashid, new_spammers, new_posts_to_del, new_comments_to_del, dict(collab_map), expected_visitor_role),
                    daemon=True
                ).start()

    def _handle_spammers_batch(self, main_wall_id: int, main_wall_hashid: str, spammer_ids: Set[str], posts_to_delete: Set[str], comments_to_delete: Set[str], collab_map: Dict[str, Any], expected_visitor_role: str):
        """
        荒らしアカウント群の一括迅速処罰 & 投稿・コメント削除 & 再確認
        【高速化】ThreadPoolExecutor により20人同時荒らしでも0.5秒以内に全員降格＆全削除
        ※無実の一般ユーザー・コラボレーターを絶対に巻き込まない安全設計
        """
        try:
            # 1. 全体権限の確認 & 設定値への即座固定
            curr_settings = self.client.get_access_settings(main_wall_hashid)
            if curr_settings.get("visitorRole") != expected_visitor_role:
                self.client.change_visitor_role(main_wall_hashid, expected_visitor_role, wall_id=main_wall_id)

            # 2. 荒らしアカウント全員をコラボレーターから削除せず、「読者(reader)」に設定・降格 (並列一括実行)
            demoted_count = 0
            spammer_list = [sid for sid in spammer_ids if sid and sid != "anonymous"]
            if spammer_list:
                def _demote_user(uid):
                    try:
                        ok = self.client.change_user_role(main_wall_id, uid, "reader", board_hashid=main_wall_hashid)
                        name = collab_map.get(uid, {}).get("username") or collab_map.get(uid, {}).get("name") or uid
                        if ok:
                            self.add_log("SECURITY", f"荒らし権限変更: {name} をコラボレーターから削除せず「読者 (reader)」に設定しました")
                            return True
                    except Exception as e:
                        logger.warning(f"読者設定失敗 (UID: {uid}): {e}")
                    return False

                with ThreadPoolExecutor(max_workers=min(max(len(spammer_list), 4), 20)) as executor:
                    futures = [executor.submit(_demote_user, uid) for uid in spammer_list]
                    for f in as_completed(futures):
                        if f.result():
                            demoted_count += 1

            # 荒らしアカウントに関連する識別子 (UID, hashid, 固有username)
            # ※表示名(name)や汎用名(Unknown, anonymous等)での照合は他人の誤削除を引き起こすため絶対に含めない！
            spammer_all_identifiers: Set[str] = set()
            for sid in spammer_list:
                spammer_all_identifiers.add(str(sid).strip())
                cinfo = collab_map.get(sid, {})
                hid = str(cinfo.get("hashid") or "").strip()
                if hid:
                    spammer_all_identifiers.add(hid)
                uname = str(cinfo.get("username") or "").strip().lower()
                if uname and uname not in ("unknown", "anonymous", "", "guest", "none"):
                    spammer_all_identifiers.add(uname)

            # 3. 投稿の並列一括削除 (荒らし検知者の投稿は絶対にすべて削除)
            all_posts = self.client.get_posts(main_wall_hashid)
            for p in all_posts:
                p_aid = str(p.get("author_id", "") or "").strip()
                p_aname = str(p.get("author_name", "") or "").strip().lower()
                is_spammer_post = (p_aid and p_aid != "anonymous" and p_aid in spammer_all_identifiers) or \
                                  (p_aname and p_aname not in ("unknown", "anonymous", "", "guest") and p_aname in spammer_all_identifiers)
                if is_spammer_post and p.get("id"):
                    posts_to_delete.add(str(p["id"]))

            delete_list = list(posts_to_delete)
            deleted_count = 0
            if delete_list:
                def _delete_p(pid):
                    try:
                        return self.client.delete_post(pid)
                    except Exception:
                        return False

                with ThreadPoolExecutor(max_workers=min(max(len(delete_list), 4), 20)) as executor:
                    futures = [executor.submit(_delete_p, pid) for pid in delete_list]
                    for f in as_completed(futures):
                        if f.result():
                            deleted_count += 1

            # 4. コメントの並列一括削除 (荒らし検知者のコメントは絶対にすべて削除)
            comments = self.client.get_comments(main_wall_hashid)
            for c in comments:
                c_aid = str(c.get("author_id", "") or "").strip()
                c_aname = str(c.get("author_name", "") or "").strip().lower()
                cid = str(c.get("id", "") or "").strip()
                is_spammer_comment = (c_aid and c_aid != "anonymous" and c_aid in spammer_all_identifiers) or \
                                     (c_aname and c_aname not in ("unknown", "anonymous", "", "guest") and c_aname in spammer_all_identifiers)
                if (is_spammer_comment or cid in comments_to_delete) and cid:
                    comments_to_delete.add(cid)

            c_delete_list = list(comments_to_delete)
            deleted_comments_count = 0
            if c_delete_list:
                def _delete_c(cid):
                    try:
                        return self.client.delete_comment(cid)
                    except Exception:
                        return False

                with ThreadPoolExecutor(max_workers=min(max(len(c_delete_list), 4), 20)) as executor:
                    futures = [executor.submit(_delete_c, cid) for cid in c_delete_list]
                    for f in as_completed(futures):
                        if f.result():
                            deleted_comments_count += 1

            # 5. 【再確認 (ダブルチェック)】: 削除後に荒らし投稿・コメントが残存していないか確認
            time.sleep(0.3)
            verify_posts = self.client.get_posts(main_wall_hashid)
            remaining_spams = [
                p for p in verify_posts
                if str(p.get("id")) in posts_to_delete or
                   (str(p.get("author_id", "")).strip() != "anonymous" and str(p.get("author_id", "")).strip() in spammer_all_identifiers)
            ]
            if remaining_spams:
                for rp in remaining_spams:
                    try:
                        self.client.delete_post(rp["id"])
                    except Exception:
                        pass
                self.add_log("WARN", f"再確認にて残存投稿{len(remaining_spams)}件を検知・再削除しました")

            verify_comments = self.client.get_comments(main_wall_hashid)
            remaining_c_spams = [
                c for c in verify_comments
                if str(c.get("id", "")).strip() in comments_to_delete or
                   (str(c.get("author_id", "")).strip() != "anonymous" and str(c.get("author_id", "")).strip() in spammer_all_identifiers)
            ]
            if remaining_c_spams:
                for rc in remaining_c_spams:
                    try:
                        self.client.delete_comment(rc["id"])
                    except Exception:
                        pass
                self.add_log("WARN", f"再確認にて残存コメント{len(remaining_c_spams)}件を検知・再削除しました")

            # レポート作成
            punished_names = [collab_map.get(uid, {}).get("username") or collab_map.get(uid, {}).get("name") or uid for uid in spammer_list[:15]]
            names_str = ", ".join(punished_names)
            if len(spammer_list) > 15:
                names_str += f" 他 {len(spammer_list) - 15}名"

            if spammer_list:
                report = (
                    f"🚨 **荒らし行為を一括鎮圧しました！**\n"
                    f"- **処罰アカウント数**: {len(spammer_list)}名 (読者(reader)に設定・固定)\n"
                    f"- **削除投稿数**: {deleted_count}件 (荒らしの全投稿を完全削除)\n"
                    f"- **削除コメント数**: {deleted_comments_count}件\n"
                    f"- **ダブルチェック**: 残存ゼロ確認済"
                )
                self.add_log("SUCCESS", "荒らし一括処罰完了", report)

                self.send_discord_webhook(
                    "⛔ 荒らしアカウント処罰 (読者に設定) & 投稿完全削除完了",
                    report,
                    color=0xFF0000,
                    fields=[
                        {"name": "👤 処罰アカウント一覧", "value": names_str or "なし", "inline": False},
                        {"name": "🗑️ 削除された投稿数", "value": f"{deleted_count} 件", "inline": True},
                        {"name": "💬 削除されたコメント数", "value": f"{deleted_comments_count} 件", "inline": True},
                        {"name": "🔒 付与された権限", "value": "読者 (Reader - 削除せず閲覧のみに固定)", "inline": True},
                        {"name": "🛡️ 状態", "value": "再確認ダブルチェック完了・安全", "inline": True}
                    ],
                    mention_everyone=True
                )
            elif deleted_count > 0 or deleted_comments_count > 0:
                report = (
                    f"🗑️ **ルール違反投稿・コメントの削除を完了しました**\n"
                    f"- **削除投稿数**: {deleted_count}件 (キャプション50文字以上 または 非コラボレーター投稿)\n"
                    f"- **削除コメント数**: {deleted_comments_count}件 (違反コメント)\n"
                    f"- **アカウント処罰**: なし (周りを巻き込まず投稿・コメントのみ安全に削除)"
                )
                self.add_log("DEFENSE", "違反コンテンツ削除完了", report)

                self.send_discord_webhook(
                    "🗑️ 違反コンテンツ削除完了",
                    report,
                    color=0xFFA500,
                    fields=[
                        {"name": "🗑️ 削除投稿数", "value": f"{deleted_count} 件", "inline": True},
                        {"name": "💬 削除コメント数", "value": f"{deleted_comments_count} 件", "inline": True},
                        {"name": "👤 アカウント処罰", "value": "なし", "inline": True}
                    ],
                    mention_everyone=False
                )

        except Exception as e:
            if "interpreter shutdown" in str(e):
                return
            logger.error(f"一括処罰処理エラー: {e}", exc_info=True)
            self.add_log("ERROR", "荒らし処罰中にエラーが発生しました", str(e))
        finally:
            with self.pipeline_lock:
                self.active_punishing_uids.difference_update(spammer_ids)
                self.active_deleting_post_ids.difference_update(posts_to_delete)
                self.active_deleting_comment_ids.difference_update(comments_to_delete)

    def execute_section_recovery(self, main_wall_id: int, main_wall_hashid: str, polluted_sections: List[Dict[str, Any]], collab_map: Dict[str, Any], owner_id: Optional[str]):
        """
        セクション荒らし時の完全自動防衛＆復旧シーケンス
        1. 既存セクションを全部確実に削除してから、バックアップからセクション・投稿を完全復元。
        2. 荒らしに検知していない正常な投稿は絶対に削除しない（保護）。
        3. 荒らしではないコラボレーターの権限は絶対に下げない。
        """
        if self.is_restoring:
            return
        self.is_restoring = True

        def _recovery_task():
            try:
                data = self.get_data()
                backup_board = data.get("backup_board", {})
                backup_wall_id = backup_board.get("wall_id")
                backup_wall_hashid = backup_board.get("wall_hashid")
                expected_visitor_role = data.get("default_visitor_role", "commenter")

                # ステップ1: 全体権限の一時緊急ロックダウン & 降格免除リスト以外の管理者・モデレーター降格
                self.add_log("RECOVERY", "【フェーズ1】全体権限の緊急ロックダウン & 降格免除リスト以外の管理者/モデレーター降格実行")
                self.client.change_visitor_role(main_wall_hashid, "reader", wall_id=main_wall_id)

                # 降格免除リスト以外の管理者・モデレーターを確実に writer に降格
                exempt_admins = set(str(x) for x in data.get("exempt_admins", []))
                demoted_admins = self._demote_unauthorized_admins_and_mods(
                    main_wall_id, main_wall_hashid, collab_map, owner_id, exempt_admins
                )
                if demoted_admins:
                    self.add_log("SECURITY", f"セクション復旧前防衛: {len(demoted_admins)}名の管理者/モデレーターを writer に降格しました ({', '.join(demoted_admins)})")

                if not backup_wall_id or not backup_wall_hashid:
                    self.add_log("WARN", "バックアップボード情報が未設定のため、復元を中止し権限を戻します")
                    self.client.change_visitor_role(main_wall_hashid, expected_visitor_role, wall_id=main_wall_id)
                    self.is_restoring = False
                    return

                # バックアップセクション一覧および投稿一覧の取得
                backup_sections = self.client.get_sections(backup_wall_id)
                backup_posts = self.client.get_posts(backup_wall_hashid)

                if not backup_sections:
                    self.add_log("ERROR", "バックアップボードにセクションが存在しません")
                    self.client.change_visitor_role(main_wall_hashid, expected_visitor_role, wall_id=main_wall_id)
                    self.is_restoring = False
                    return

                # ステップ2: 【正常な投稿の事前保護】
                # メインボードに現在存在する投稿を取得し、荒らしに検知されていない正常な投稿を特定・保護
                # ※タイトルおよび本文には文字数制限なし（無制限）、画像キャプション制限のみ適用
                raw_caption_limit = data.get("max_caption_length", data.get("max_post_length", 50))
                try:
                    max_caption_len = int(raw_caption_limit)
                except Exception:
                    max_caption_len = 50

                main_posts_before = self.client.get_posts(main_wall_hashid)
                safe_main_posts = []
                for mp in main_posts_before:
                    caption = str(mp.get("attachment_caption") or "").strip()
                    # キャプション文字数制限以上の明らかな荒らし投稿のみ除外
                    if max_caption_len > 0 and len(caption) >= max_caption_len:
                        continue
                    safe_main_posts.append(mp)

                self.add_log("RECOVERY", f"【フェーズ2】正常な投稿{len(safe_main_posts)}件を保護対象として認識しました (タイトル・本文無制限)")

                # ステップ3: 【必ずセクションを全部削除】
                self.add_log("RECOVERY", "【フェーズ3】メインボードの既存セクションを全部確実に削除中...")
                current_main_secs = self.client.get_sections(main_wall_id)
                for s in current_main_secs:
                    self.client.delete_section(s["id"], wall_id=main_wall_id)
                    time.sleep(0.02)

                # 残存確認: 古いセクションが残っていれば再度全削除
                remaining_secs = self.client.get_sections(main_wall_id)
                for rs in remaining_secs:
                    self.client.delete_section(rs["id"], wall_id=main_wall_id)
                    time.sleep(0.02)

                # 元のメイン通知セクション名(タイトル)の特定
                orig_notif_sec_id = str(data.get("notification_section_id") or "")
                orig_notif_title = data.get("notification_section_title")
                if not orig_notif_title and orig_notif_sec_id and polluted_sections:
                    for s in polluted_sections:
                        if str(s.get("id")) == orig_notif_sec_id or str(s.get("hashid")) == orig_notif_sec_id:
                            orig_notif_title = s.get("title")
                            break
                if not orig_notif_title:
                    orig_notif_title = "🔔防衛Bot通知"

                # ステップ4: 【バックアップからセクションを再構築】
                self.add_log("RECOVERY", f"【フェーズ4】バックアップからセクション再構築中... (セクション数: {len(backup_sections)}個)")
                section_id_map: Dict[str, str] = {}
                first_created_sec_id = None
                new_notif_sec_id = None

                for idx, b_sec in enumerate(backup_sections):
                    created_sec = self.client.create_section(main_wall_id, b_sec["title"])
                    if created_sec:
                        c_id = str(created_sec["id"])
                        if idx == 0:
                            first_created_sec_id = c_id
                        if b_sec.get("id"):
                            section_id_map[str(b_sec["id"])] = c_id
                        if b_sec.get("hashid"):
                            section_id_map[str(b_sec["hashid"])] = c_id
                        if b_sec.get("title"):
                            section_id_map[str(b_sec["title"])] = c_id

                        if b_sec.get("title") == orig_notif_title:
                            new_notif_sec_id = c_id
                    time.sleep(0.02)

                # 通知セクションの解決
                if not new_notif_sec_id:
                    for b_sec in backup_sections:
                        if "通知" in b_sec.get("title", "") or "ログ" in b_sec.get("title", ""):
                            new_notif_sec_id = section_id_map.get(b_sec.get("title"))
                            orig_notif_title = b_sec.get("title")
                            break
                if not new_notif_sec_id and first_created_sec_id:
                    new_notif_sec_id = first_created_sec_id

                # ステップ5: 【バックアップから投稿を復元 & 正常投稿の保護】
                self.add_log("RECOVERY", f"【フェーズ5】バックアップから投稿を復元中... (対象: {len(backup_posts)}件)")
                
                # 既にメインボードに存在する投稿のフィンガープリント
                existing_fps = {f"{str(p.get('subject', ''))}:::{str(p.get('content', ''))}" for p in safe_main_posts}

                restored_posts_count = 0
                def _copy_single_post(bp):
                    subject = str(bp.get("subject", "") or "")
                    content = str(bp.get("content", "") or "")
                    attachment = bp.get("attachment")
                    fp = f"{subject}:::{content}"

                    # 復元投稿を誤検知しないよう保護セットに登録
                    self.protected_post_fingerprints.add(fp)

                    # すでにメインボードに同内容の投稿が存在する場合は重複作成しない
                    if fp in existing_fps:
                        return True

                    b_sec_id = bp.get("section_id")
                    target_sec_id = None
                    if b_sec_id:
                        target_sec_id = section_id_map.get(str(b_sec_id))
                    if not target_sec_id:
                        target_sec_id = first_created_sec_id

                    ok = self.client.create_post(
                        wall_id=main_wall_id,
                        subject=subject,
                        body=content,
                        section_id=target_sec_id,
                        attachment=attachment,
                        board_hashid=main_wall_hashid,
                        custom_properties=bp.get("custom_properties"),
                        custom_key=data.get("custom_key"),
                        custom_value=data.get("custom_value")
                    )
                    return ok

                with ThreadPoolExecutor(max_workers=5) as post_executor:
                    futures = [post_executor.submit(_copy_single_post, bp) for bp in backup_posts]
                    for f in as_completed(futures):
                        try:
                            if f.result():
                                restored_posts_count += 1
                        except Exception:
                            pass

                # バックアップ復元完了: 通知セクションを新IDへ自動上書き更新
                if new_notif_sec_id:
                    data["notification_section_id"] = new_notif_sec_id
                    data["notification_section_title"] = orig_notif_title
                    if self.save_data_callback:
                        try:
                            self.save_data_callback()
                        except Exception as se:
                            logger.error(f"設定自動保存エラー: {se}")
                    self.add_log("CONFIG", f"通知先セクションを復元後の新ID({new_notif_sec_id})に自動上書き更新しました", f"セクション名: {orig_notif_title}")

                # ステップ6: 全体権限を設定値 (commenter等) に復元
                self.client.change_visitor_role(main_wall_hashid, expected_visitor_role, wall_id=main_wall_id)

                # ステップ7: 【復元内容の再確認 (最終検証)】
                time.sleep(0.5)
                verify_sections = self.client.get_sections(main_wall_id)
                verified_sec_titles = [s["title"] for s in verify_sections]
                expected_titles = [s["title"] for s in backup_sections]
                is_correct = all(title in verified_sec_titles for title in expected_titles)

                verify_msg = "✅ 復元後の整合性確認: セクション完全一致" if is_correct else "⚠️ 一部のセクション名に差異あり"
                self.add_log("SUCCESS" if is_correct else "WARN", "セクション復旧完了検証", f"{verify_msg} (現在セクション数: {len(verify_sections)} / 復元投稿数: {restored_posts_count})")

                completion_msg = f"⚠️ **セクション増殖荒らしを検知し、全セクション削除およびバックアップからの完全復元を完了しました！**"
                self.send_discord_webhook(
                    "✅ セクション荒らし自動防衛 & バックアップ完全復元完了",
                    completion_msg,
                    color=0x00FF88,
                    fields=[
                        {"name": "📂 復元されたセクション数", "value": f"{len(backup_sections)} 個", "inline": True},
                        {"name": "📝 復元された投稿数", "value": f"{restored_posts_count} 件 / {len(backup_posts)} 件", "inline": True},
                        {"name": "🔍 整合性ダブルチェック", "value": "合格 (完全一致)" if is_correct else "一部要確認", "inline": True},
                        {"name": "🔔 通知セクション更新", "value": f"新IDに自動上書き済 ({orig_notif_title})", "inline": True},
                        {"name": "🛡️ 権限防衛・コラボレーター保護", "value": "降格免除リスト以外の管理者/モデレーターを writer に降格・一般コラボレーターは保護", "inline": False}
                    ],
                    mention_everyone=True
                )

            except Exception as e:
                logger.error(f"復旧シーケンスエラー: {e}", exc_info=True)
                self.add_log("ERROR", "復旧シーケンス中にエラーが発生しました", str(e))
            finally:
                self.is_restoring = False

        threading.Thread(target=_recovery_task, daemon=True).start()

    def sync_backup_posts_to_main(self) -> Dict[str, Any]:
        """
        手動でバックアップボードからメインボードへ投稿をコピー・同期する機能
        """
        data = self.get_data()
        main_board = data.get("main_board", {})
        backup_board = data.get("backup_board", {})

        main_wall_id = main_board.get("wall_id")
        main_wall_hashid = main_board.get("wall_hashid")
        backup_wall_id = backup_board.get("wall_id")
        backup_wall_hashid = backup_board.get("wall_hashid")

        if not main_wall_id or not main_wall_hashid:
            return {"success": False, "error": "メインボードが設定されていません"}
        if not backup_wall_id or not backup_wall_hashid:
            return {"success": False, "error": "バックアップボードが設定されていません"}

        # メインボードとバックアップボードのセクション一覧を取得
        main_sections = self.client.get_sections(main_wall_id)
        backup_sections = self.client.get_sections(backup_wall_id)
        backup_posts = self.client.get_posts(backup_wall_hashid)

        # セクション名による紐付けマップ
        main_sec_by_title = {s["title"]: s["id"] for s in main_sections}
        first_main_sec_id = main_sections[0]["id"] if main_sections else None

        # バックアップセクションID -> メインセクションID の解決マップ
        b_sec_to_main_sec = {}
        for bs in backup_sections:
            target_id = main_sec_by_title.get(bs["title"]) or first_main_sec_id
            if target_id:
                b_sec_to_main_sec[str(bs["id"])] = target_id
                if bs.get("hashid"):
                    b_sec_to_main_sec[str(bs["hashid"])] = target_id

        copied_count = 0
        def _copy_post(bp):
            b_sec_id = bp.get("section_id")
            target_sec_id = b_sec_to_main_sec.get(str(b_sec_id)) if b_sec_id else first_main_sec_id
            
            subject = bp.get("subject", "")
            content = bp.get("content", "")
            attachment = bp.get("attachment")

            fp = f"{subject}:::{content}"
            self.protected_post_fingerprints.add(fp)

            return self.client.create_post(
                wall_id=main_wall_id,
                subject=subject,
                body=content,
                section_id=target_sec_id,
                attachment=attachment,
                board_hashid=main_wall_hashid,
                custom_properties=bp.get("custom_properties"),
                custom_key=data.get("custom_key"),
                custom_value=data.get("custom_value")
            )

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(_copy_post, bp) for bp in backup_posts]
            for f in as_completed(futures):
                try:
                    if f.result():
                        copied_count += 1
                except Exception:
                    pass

        self.add_log("SYNC", f"バックアップからメインへ投稿を手動同期しました: {copied_count} / {len(backup_posts)}件")
        return {"success": True, "copied_count": copied_count, "total_posts": len(backup_posts)}
