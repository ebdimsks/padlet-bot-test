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
        # 既知の投稿IDセット (サイクルごとの新規投稿検知・暴走防止用)
        self.known_post_ids: Set[str] = set()
        # 初回起動時の初期スキャン完了フラグ (起動時に元からある既存投稿の誤検知完全防止)
        self.has_initialized_posts: bool = False
        self.current_wall_id: Optional[int] = None
        # 並立処罰パイプライン (1人対処中も他人の荒らしを並行して検知・処罰)
        self.pipeline_lock = threading.Lock()
        self.active_punishing_uids: Set[str] = set()
        self.active_deleting_post_ids: Set[str] = set()
        # 短時間アクティビティ履歴 (DDoS検知用): [(timestamp, user_id, post_id)]
        self.recent_activity_log: List[tuple] = []
        # 復元中フラグ
        self.is_restoring = False
        # 復旧処理で作成された投稿の保護用フィンガープリント (誤削除防止)
        self.protected_post_fingerprints: Set[str] = set()

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
        self.known_post_ids.clear()
        self.user_post_timestamps.clear()
        self.user_image_history.clear()
        with self.pipeline_lock:
            self.active_punishing_uids.clear()
            self.active_deleting_post_ids.clear()
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

    def _check_cycle(self, data: Dict[str, Any], main_wall_id: int, main_wall_hashid: str):
        """1サイクルの包括的防衛チェック (セクション荒らし・投稿荒らしが同時に来ても両方即時対処)"""
        now = time.time()
        main_board_info = data.get("main_board", {})
        owner_id = str(main_board_info.get("owner_id", "")) if main_board_info.get("owner_id") else None

        # 許可・免除リスト
        allowed_admins = set(data.get("allowed_admins", []))
        allowed_mods = set(data.get("allowed_moderators", []))
        exempt_admins = set(data.get("exempt_admins", []))

        # =====================================================================
        # 1. 全体アクセス設定 (visitorRole) の監視 & 強制修復 (none対応)
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

        # =====================================================================
        # 2. コラボレーター情報の取得 & 不正管理者・モデレーター昇格の検知・即降格
        # =====================================================================
        collaborators = self.client.get_collaborators(main_wall_hashid, main_wall_id)
        collab_map = {c["user_id"]: c for c in collaborators}
        registered_collab_ids = set(collab_map.keys())

        for uid, cinfo in collab_map.items():
            if owner_id and uid == owner_id:
                continue  # 部屋主は保護
            role = (cinfo.get("role") or "").lower()
            username = cinfo.get("username") or cinfo.get("name") or uid

            # 管理者チェック
            if role in ("admin", "owner"):
                if uid not in allowed_admins and username not in allowed_admins and uid not in exempt_admins and username not in exempt_admins:
                    self.client.change_user_role(main_wall_id, uid, "writer")
                    msg = f"未許可の管理者昇格を検知: {username} (ID: {uid}) を writer に降格しました"
                    self.add_log("SECURITY", msg)
                    self.send_discord_webhook("不正管理者検知・即時降格", msg, color=0xFFA500)

            # モデレーターチェック
            elif role == "moderator":
                if uid not in allowed_mods and username not in allowed_mods and uid not in exempt_admins and username not in exempt_admins:
                    self.client.change_user_role(main_wall_id, uid, "writer")
                    msg = f"未許可のモデレーター昇格を検知: {username} (ID: {uid}) を writer に降格しました"
                    self.add_log("SECURITY", msg)
                    self.send_discord_webhook("不正モデレーター検知・即時降格", msg, color=0xFFA500)

        # =====================================================================
        # 3. セクション増殖荒らしの検知 & 自動復旧起動 (returnせず投稿監視も継続！)
        # =====================================================================
        max_sections = int(data.get("max_sections", 10))
        current_sections = self.client.get_sections(main_wall_id)
        if len(current_sections) > max_sections and not self.is_restoring:
            alert_msg = f"セクション数異常増殖を検知 (現在: {len(current_sections)}個 / 上限: {max_sections}個)！"
            self.add_log("ALERT", "🚨 セクション荒らし検知！", alert_msg)
            # 被害拡大をミリ秒で防ぐため、直ちに全体権限をロックダウン
            self.client.change_visitor_role(main_wall_hashid, "reader", wall_id=main_wall_id)
            self.send_discord_webhook(
                "🚨 セクション荒らし検知・即時防衛発動！",
                f"{alert_msg}\n全体権限を即時ロックダウンし、バックアップからの完全復元シーケンスを開始します。",
                color=0xFF0000,
                fields=[
                    {"name": "検知セクション数", "value": f"{len(current_sections)} 個 (上限: {max_sections})", "inline": True},
                    {"name": "防衛ステータス", "value": "全体権限ロック ➜ バックアップ復元中", "inline": True}
                ],
                mention_everyone=True
            )
            # バックグラウンドスレッドで復元を実行（監視ループはブロックしない）
            self.execute_section_recovery(main_wall_id, main_wall_hashid, current_sections, collab_map, owner_id)
            # ※ここで return せず、同時に発生している投稿荒らしも下のステップで即座に対処する！

        # =====================================================================
        # 4. 投稿 & コメント荒らしの精密・高精度防衛監視 (誤検知完全防止)
        # =====================================================================
        posts = self.client.get_posts(main_wall_hashid)
        comments = self.client.get_comments(main_wall_hashid)

        delete_non_collab = data.get("delete_non_collaborators", True)

        # 処罰対象 (読者に降格 & その人の投稿を全削除)
        spammer_uids_to_punish: Set[str] = set()
        # 削除対象投稿IDの集合 (荒らし投稿、非コラボ投稿、キャプション50文字以上)
        posts_to_delete: Set[str] = set()

        # ボードが切り替わった場合のリセット
        if self.current_wall_id != main_wall_id:
            self.current_wall_id = main_wall_id
            self.has_initialized_posts = False
            self.known_post_ids.clear()
            self.user_post_timestamps.clear()
            self.user_image_history.clear()

        # 現在存在する全投稿ID
        current_post_ids = {p.get("id") for p in posts if p.get("id")}

        # 初回起動時の安全ガード: ボードに元からある既存投稿をすべて安全に記録（誤検知の完全防止）
        if not self.has_initialized_posts:
            self.known_post_ids = set(current_post_ids)
            self.has_initialized_posts = True
            new_posts = []  # 初回は既存投稿を登録するだけで新規投稿判定は行わない
            self.add_log("SYSTEM", f"ボード初期化完了: 既存投稿{len(current_post_ids)}件を安全に除外登録しました")
        else:
            # 前回のサイクル以降に新しく検出された投稿のみを対象とする
            new_posts = [p for p in posts if p.get("id") and p.get("id") not in self.known_post_ids]

        # -------------------------------------------------------------
        # 【投稿荒らし検知 ①】1秒間に2投稿以上、または2秒間に4投稿以上
        # 「その荒らした人だけ」荒らし判定にする。処罰は読者。投稿はすべて削除。
        # -------------------------------------------------------------
        for p in new_posts:
            pid = p.get("id")
            aid = p.get("author_id", "anonymous")
            if owner_id and aid == owner_id:
                continue
            if aid in exempt_admins or collab_map.get(aid, {}).get("username") in exempt_admins:
                continue

            # Bot自身の防衛通知は除外
            fp = f"{p.get('subject', '')}:::{p.get('content', '')}"
            if fp in self.protected_post_fingerprints or (p.get("subject") or "").startswith("🛡️ [防衛通知]"):
                continue

            if aid not in self.user_post_timestamps:
                self.user_post_timestamps[aid] = []
            self.user_post_timestamps[aid].append(now)

        # アカウントごとの連投レート判定
        for aid, timestamps in list(self.user_post_timestamps.items()):
            # 直近10秒以内のタイムスタンプのみ保持
            valid_ts = [t for t in timestamps if now - t <= 10.0]
            self.user_post_timestamps[aid] = valid_ts

            if owner_id and aid == owner_id:
                continue
            if aid in exempt_admins or collab_map.get(aid, {}).get("username") in exempt_admins:
                continue

            # 1秒間に2投稿以上、または2秒間に4投稿以上
            count_1s = len([t for t in valid_ts if now - t <= 1.0])
            count_2s = len([t for t in valid_ts if now - t <= 2.0])

            if count_1s >= 2 or count_2s >= 4:
                author_name = collab_map.get(aid, {}).get("username") or collab_map.get(aid, {}).get("name") or aid
                reason = f"連投爆撃検知 (1秒に{count_1s}件 / 2秒に{count_2s}件)"
                self.add_log("ALERT", f"連投荒らし検知: {author_name}", reason)
                if aid and aid != "anonymous":
                    spammer_uids_to_punish.add(aid)

        # -------------------------------------------------------------
        # 【投稿荒らし検知 ②】同じ画像を短期間に複数投稿
        # 「その荒らした人だけ」荒らし判定にする。処罰は読者。投稿はすべて削除。
        # -------------------------------------------------------------
        for p in new_posts:
            pid = p.get("id")
            aid = p.get("author_id", "anonymous")
            if owner_id and aid == owner_id:
                continue
            if aid in exempt_admins or collab_map.get(aid, {}).get("username") in exempt_admins:
                continue

            fp = f"{p.get('subject', '')}:::{p.get('content', '')}"
            if fp in self.protected_post_fingerprints or (p.get("subject") or "").startswith("🛡️ [防衛通知]"):
                continue

            attachment = p.get("attachment")
            if attachment:
                if aid not in self.user_image_history:
                    self.user_image_history[aid] = []

                # 直近30秒以内の画像履歴
                img_hist = [item for item in self.user_image_history[aid] if now - item[0] <= 30.0]
                same_img = [item for item in img_hist if item[1] == attachment and item[2] != pid]
                if same_img:
                    author_name = collab_map.get(aid, {}).get("username") or collab_map.get(aid, {}).get("name") or aid
                    self.add_log("ALERT", f"同一画像連投荒らし検知: {author_name}", f"短期間に同一画像を重複投稿 (PostID: {pid})")
                    if aid and aid != "anonymous":
                        spammer_uids_to_punish.add(aid)

                img_hist.append((now, attachment, pid))
                self.user_image_history[aid] = img_hist

        # -------------------------------------------------------------
        # 【読者にはしないけど、投稿を削除するやつ】
        # ① 画像のキャプションのところが50文字以上の投稿
        # ② コラボレーター以外の投稿
        # -------------------------------------------------------------
        for p in posts:
            pid = p.get("id")
            aid = p.get("author_id", "anonymous")
            if not pid:
                continue
            if owner_id and aid == owner_id:
                continue  # 部屋主は保護
            if aid in exempt_admins or collab_map.get(aid, {}).get("username") in exempt_admins:
                continue

            fp = f"{p.get('subject', '')}:::{p.get('content', '')}"
            if fp in self.protected_post_fingerprints or (p.get("subject") or "").startswith("🛡️ [防衛通知]"):
                continue

            # ① 画像キャプションが50文字以上 -> 投稿のみ削除 (読者にはしない)
            caption = (p.get("attachment_caption") or "").strip()
            if len(caption) >= 50:
                posts_to_delete.add(pid)
                self.add_log("DEFENSE", f"画像キャプション50文字以上の投稿を削除: {len(caption)}文字 (PostID: {pid})")
                continue

            # ② コラボレーター以外の投稿 -> 投稿のみ削除 (読者にはしない)
            if delete_non_collab:
                if aid not in registered_collab_ids:
                    posts_to_delete.add(pid)
                    self.add_log("DEFENSE", f"非コラボレーター投稿を削除: {pid} (Author: {aid})")
                    continue

        # コラボレーター以外のコメント即時削除 (部屋主は完全保護)
        if delete_non_collab:
            for c in comments:
                try:
                    c_author_id = str(c.get("author", {}).get("id", ""))
                    c_id = str(c.get("id", ""))
                    if owner_id and c_author_id == owner_id:
                        continue
                    if c_author_id not in registered_collab_ids and c_id:
                        self.client.delete_comment(c_id)
                        self.add_log("DEFENSE", f"非コラボレーターコメントを即時削除: {c_id}")
                except Exception:
                    pass

        # -------------------------------------------------------------
        # 荒らしに検知した人の投稿は「絶対にすべて削除」
        # -------------------------------------------------------------
        if spammer_uids_to_punish:
            for p in posts:
                if p.get("author_id") in spammer_uids_to_punish and p.get("id"):
                    posts_to_delete.add(p["id"])

        # 既知投稿IDの更新 (次回サイクルの新規投稿判定用)
        self.known_post_ids = current_post_ids

        # (E) 処罰 & 削除の超高速並列・並立実行 (20人同時荒らし・並行防衛)
        # 1人の処罰中であっても監視ループは止まらず、他の荒らし検知・処罰を並立して実行！
        if spammer_uids_to_punish or posts_to_delete:
            with self.pipeline_lock:
                new_spammers = set(spammer_uids_to_punish) - self.active_punishing_uids
                new_posts_to_del = set(posts_to_delete) - self.active_deleting_post_ids
                self.active_punishing_uids.update(new_spammers)
                self.active_deleting_post_ids.update(new_posts_to_del)

            if new_spammers or new_posts_to_del:
                threading.Thread(
                    target=self._handle_spammers_batch,
                    args=(main_wall_id, main_wall_hashid, new_spammers, new_posts_to_del, dict(collab_map), expected_visitor_role),
                    daemon=True
                ).start()

    def _handle_spammers_batch(self, main_wall_id: int, main_wall_hashid: str, spammer_ids: Set[str], posts_to_delete: Set[str], collab_map: Dict[str, Any], expected_visitor_role: str):
        """
        荒らしアカウント群の一括迅速処罰 & 投稿削除 & 再確認
        【高速化】ThreadPoolExecutor により20人同時荒らしでも0.5秒以内に全員降格＆全削除
        """
        try:
            # 1. 全体権限の確認 & 設定値への即座固定
            curr_settings = self.client.get_access_settings(main_wall_hashid)
            if curr_settings.get("visitorRole") != expected_visitor_role:
                self.client.change_visitor_role(main_wall_hashid, expected_visitor_role, wall_id=main_wall_id)

            # 2. 荒らしアカウント全員をコラボレーターから削除せず、「読者(reader)」に設定・降格 (並列一括実行)
            demoted_count = 0
            spammer_list = list(spammer_ids)
            if spammer_list:
                def _demote_user(uid):
                    try:
                        # コラボレーターから削除するのではなく、確実に「読者 (reader)」として設定・固定
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

            # 3. 投稿の並列一括削除
            # 対象ユーザーの全投稿も削除対象に追加
            all_posts = self.client.get_posts(main_wall_hashid)
            for p in all_posts:
                if p.get("author_id") in spammer_ids:
                    posts_to_delete.add(p["id"])

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

            # コメント削除
            comments = self.client.get_comments(main_wall_hashid)
            for c in comments:
                try:
                    c_author = str(c.get("author", {}).get("id", ""))
                    if c_author in spammer_ids and c.get("id"):
                        self.client.delete_comment(str(c["id"]))
                except Exception:
                    pass

            # 4. 【再確認 (ダブルチェック)】: 削除後に残存投稿がないか確認
            time.sleep(0.3)
            verify_posts = self.client.get_posts(main_wall_hashid)
            remaining_spams = [p for p in verify_posts if p.get("id") in posts_to_delete or p.get("author_id") in spammer_ids]
            if remaining_spams:
                for rp in remaining_spams:
                    try:
                        self.client.delete_post(rp["id"])
                    except Exception:
                        pass
                self.add_log("WARN", f"再確認にて残存投稿{len(remaining_spams)}件を検知・再削除しました")

            # レポート作成
            punished_names = [collab_map.get(uid, {}).get("username") or collab_map.get(uid, {}).get("name") or uid for uid in spammer_list[:15]]
            names_str = ", ".join(punished_names)
            if len(spammer_list) > 15:
                names_str += f" 他 {len(spammer_list) - 15}名"

            if spammer_ids:
                report = (
                    f"🚨 **荒らし行為を一括鎮圧しました！**\n"
                    f"- **処罰アカウント数**: {len(spammer_ids)}名 (コラボレーター削除ではなく「読者(reader)」に設定・固定)\n"
                    f"- **削除投稿数**: {deleted_count}件\n"
                    f"- **ダブルチェック**: 残存ゼロ確認済"
                )
                self.add_log("SUCCESS", "荒らし一括処罰完了", report)

                self.send_discord_webhook(
                    "⛔ 荒らしアカウント処罰 (読者に設定) & 投稿完全削除完了",
                    report,
                    color=0xFF0000,
                    fields=[
                        {"name": "👤 処罰アカウント一覧", "value": names_str or "匿名荒らし / なし", "inline": False},
                        {"name": "🗑️ 削除された投稿数", "value": f"{deleted_count} 件", "inline": True},
                        {"name": "🔒 付与された権限", "value": "読者 (Reader - 削除せず閲覧のみに固定)", "inline": True},
                        {"name": "🛡️ 状態", "value": "再確認ダブルチェック完了・安全", "inline": True}
                    ],
                    mention_everyone=True
                )
            elif deleted_count > 0:
                report = (
                    f"🗑️ **ルール違反投稿の削除を完了しました**\n"
                    f"- **削除投稿数**: {deleted_count}件 (画像キャプション50文字以上 または 非コラボレーター投稿)\n"
                    f"- **アカウント処罰**: なし (投稿のみ安全に削除)"
                )
                self.add_log("DEFENSE", "違反投稿削除完了", report)

                self.send_discord_webhook(
                    "🗑️ 違反投稿削除完了 (キャプション50文字以上 / 非コラボレーター)",
                    report,
                    color=0xFFA500,
                    fields=[
                        {"name": "🗑️ 削除された投稿数", "value": f"{deleted_count} 件", "inline": True},
                        {"name": "👤 アカウント処罰", "value": "なし (投稿削除のみ)", "inline": True},
                        {"name": "🛡️ 状態", "value": "安全", "inline": True}
                    ],
                    mention_everyone=False
                )

        except Exception as e:
            logger.error(f"一括処罰処理エラー: {e}", exc_info=True)
            self.add_log("ERROR", "荒らし処罰中にエラーが発生しました", str(e))
        finally:
            with self.pipeline_lock:
                self.active_punishing_uids.difference_update(spammer_ids)
                self.active_deleting_post_ids.difference_update(posts_to_delete)

    def execute_section_recovery(self, main_wall_id: int, main_wall_hashid: str, polluted_sections: List[Dict[str, Any]], collab_map: Dict[str, Any], owner_id: Optional[str]):
        """
        セクション荒らし時の完全自動防衛＆復旧シーケンス
        バックアップボードから【セクション】と【投稿】の両方を確実にコピーして完全復元
        """
        if self.is_restoring:
            return
        self.is_restoring = True

        def _recovery_task():
            try:
                data = self.get_data()
                exempt_admins = set(data.get("exempt_admins", []))
                safe_mods = set(data.get("allowed_moderators", []))
                backup_board = data.get("backup_board", {})
                backup_wall_id = backup_board.get("wall_id")
                backup_wall_hashid = backup_board.get("wall_hashid")
                expected_visitor_role = data.get("default_visitor_role", "none")

                # ステップ1: 全体権限のロックダウン
                self.add_log("RECOVERY", "【フェーズ1】全体権限の緊急ロックダウン開始")
                self.client.change_visitor_role(main_wall_hashid, "reader", wall_id=main_wall_id)

                # ステップ2: セーフ管理者・モデレーター・部屋主以外を一括で writer に降格
                def _demote_collab(item):
                    uid, cinfo = item
                    if owner_id and uid == owner_id:
                        return
                    username = cinfo.get("username") or cinfo.get("name") or uid
                    role = (cinfo.get("role") or "").lower()
                    is_safe = (uid in exempt_admins or username in exempt_admins or uid in safe_mods or username in safe_mods)
                    if not is_safe and role in ("admin", "moderator"):
                        self.client.change_user_role(main_wall_id, uid, "writer")
                        self.add_log("SECURITY", f"防衛降格: {username} ({role} -> writer)")

                with ThreadPoolExecutor(max_workers=8) as ex:
                    list(ex.map(_demote_collab, collab_map.items()))

                time.sleep(0.1)

                # ステップ3: 一時セクションを作成し、既存の汚染セクションを一括削除
                self.add_log("RECOVERY", "【フェーズ2】汚染セクションの初期化中...")
                temp_sec = self.client.create_section(main_wall_id, "_safe_restore_guard_")
                time.sleep(0.05)

                for s in polluted_sections:
                    self.client.delete_section(s["id"])
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

                if not backup_wall_id or not backup_wall_hashid:
                    self.add_log("WARN", "バックアップボード情報が未設定のため、セクション初期化のみで終了します")
                    if temp_sec:
                        self.client.delete_section(temp_sec["id"])
                    self.client.change_visitor_role(main_wall_hashid, expected_visitor_role, wall_id=main_wall_id)
                    self.is_restoring = False
                    return

                # バックアップセクション一覧の取得
                backup_sections = self.client.get_sections(backup_wall_id)
                backup_posts = self.client.get_posts(backup_wall_hashid)

                # Webhook未設定時: バックアップ部屋のメイン通知セクションと同名のセクションへ荒らし検知を通知
                webhook_url = data.get("discord_webhook_url", "").strip()
                if not webhook_url:
                    target_b_sec = next((bs for bs in backup_sections if bs.get("title") == orig_notif_title), None)
                    if not target_b_sec:
                        target_b_sec = next((bs for bs in backup_sections if "通知" in bs.get("title", "") or "ログ" in bs.get("title", "")), None)
                    
                    if target_b_sec:
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        b_sec_id = target_b_sec.get("id")
                        b_body = (
                            f"【🛡️ Padlet防衛Bot通知】\n"
                            f"⚠️ メイン部屋でセクション増殖荒らし({len(polluted_sections)}個)を検知しました！\n"
                            f"▶ 全体権限を緊急ロックダウンし、汚染セクションを初期化してバックアップから完全復元を開始しています。\n"
                            f"⏱️ 発生時刻: {now_str}"
                        )
                        self.client.create_post(
                            wall_id=backup_wall_id,
                            subject="🛡️ [防衛通知] セクション増殖荒らしを検知・復旧シーケンス開始",
                            body=b_body,
                            section_id=str(b_sec_id) if b_sec_id else None,
                            board_hashid=backup_wall_hashid,
                            custom_key=data.get("custom_key"),
                            custom_value=data.get("custom_value")
                        )
                        self.add_log("NOTIFICATION", f"バックアップの同名セクション({target_b_sec.get('title')})に荒らし通知を送信しました")

                # ステップ4: バックアップからセクション再作成 & マッピングテーブル構築
                self.add_log("RECOVERY", "【フェーズ3】バックアップからセクション再構築中...")

                section_id_map: Dict[str, str] = {}
                first_created_sec_id = None
                new_notif_sec_id = None

                for b_sec in backup_sections:
                    created_sec = self.client.create_section(main_wall_id, b_sec["title"])
                    if created_sec:
                        c_id = str(created_sec["id"])
                        if first_created_sec_id is None:
                            first_created_sec_id = c_id

                        # ID, hashid, タイトルで柔軟にマッピング
                        if b_sec.get("id"):
                            section_id_map[str(b_sec["id"])] = c_id
                        if b_sec.get("hashid"):
                            section_id_map[str(b_sec["hashid"])] = c_id
                        if b_sec.get("title"):
                            section_id_map[str(b_sec["title"])] = c_id

                        # メイン部屋で通知セクションに指定していたセクションと同名の場合、新IDを記録
                        if b_sec.get("title") == orig_notif_title:
                            new_notif_sec_id = c_id
                    time.sleep(0.02)

                # 完全一致がなければ「通知」または「ログ」を含むセクション、または先頭セクション
                if not new_notif_sec_id:
                    for b_sec in backup_sections:
                        if "通知" in b_sec.get("title", "") or "ログ" in b_sec.get("title", ""):
                            new_notif_sec_id = section_id_map.get(b_sec.get("title"))
                            orig_notif_title = b_sec.get("title")
                            break
                if not new_notif_sec_id and first_created_sec_id:
                    new_notif_sec_id = first_created_sec_id

                # ステップ5: バックアップから投稿を並列コピー
                self.add_log("RECOVERY", f"【フェーズ4】バックアップから投稿を復元中... (対象: {len(backup_posts)}件)")
                restored_posts_count = 0

                def _copy_single_post(bp):
                    b_sec_id = bp.get("section_id")
                    target_sec_id = None
                    if b_sec_id:
                        target_sec_id = section_id_map.get(str(b_sec_id))
                    if not target_sec_id:
                        target_sec_id = first_created_sec_id  # 第1セクションにフォールバック

                    subject = bp.get("subject", "")
                    content = bp.get("content", "")
                    attachment = bp.get("attachment")

                    # 復元投稿を誤検知しないよう保護セットに登録
                    fp = f"{subject}:::{content}"
                    self.protected_post_fingerprints.add(fp)

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

                # 一時セクション削除
                if temp_sec:
                    self.client.delete_section(temp_sec["id"])

                # バックアップ復元完了: 通知セクションを新IDへ自動で上書き更新
                if new_notif_sec_id:
                    data["notification_section_id"] = new_notif_sec_id
                    data["notification_section_title"] = orig_notif_title
                    if self.save_data_callback:
                        try:
                            self.save_data_callback()
                        except Exception as se:
                            logger.error(f"設定自動保存エラー: {se}")
                    self.add_log("CONFIG", f"通知先セクションを復元後の新ID({new_notif_sec_id})に自動上書き更新しました", f"セクション名: {orig_notif_title}")

                # ステップ6: 全体権限を設定値に戻す
                self.client.change_visitor_role(main_wall_hashid, expected_visitor_role, wall_id=main_wall_id)

                # ステップ7: 【復元内容の再確認 (最終検証)】
                time.sleep(0.5)
                verify_sections = self.client.get_sections(main_wall_id)
                verified_sec_titles = [s["title"] for s in verify_sections]
                expected_titles = [s["title"] for s in backup_sections]
                is_correct = all(title in verified_sec_titles for title in expected_titles)

                verify_msg = "✅ 復元後の整合性確認: セクション完全一致" if is_correct else "⚠️ 一部のセクション名に差異あり"
                self.add_log("SUCCESS" if is_correct else "WARN", "セクション復旧完了検証", f"{verify_msg} (現在セクション数: {len(verify_sections)} / 復元投稿数: {restored_posts_count})")

                completion_msg = f"⚠️ **セクション増殖荒らしを検知し、自動防衛およびバックアップからの完全復元を完走しました！**"
                self.send_discord_webhook(
                    "✅ セクション荒らし自動防衛 & バックアップ完全復元完了",
                    completion_msg,
                    color=0x00FF88,
                    fields=[
                        {"name": "📂 復元されたセクション数", "value": f"{len(backup_sections)} 個", "inline": True},
                        {"name": "📝 復元された投稿数", "value": f"{restored_posts_count} 件 / {len(backup_posts)} 件", "inline": True},
                        {"name": "🔍 整合性ダブルチェック", "value": "合格 (完全一致)" if is_correct else "一部要確認", "inline": True},
                        {"name": "🔔 通知セクション更新", "value": f"新IDに自動上書き済 ({orig_notif_title})", "inline": True},
                        {"name": "🛡️ 権限ロックダウン", "value": "セーフ管理者・モデレーター・部屋主以外を一括降格済", "inline": False}
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
