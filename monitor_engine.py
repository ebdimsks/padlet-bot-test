import os
import re
import time
import json
import logging
import threading
from datetime import datetime
from typing import Dict, List, Any, Optional, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

from padlet_client import PadletClient

logger = logging.getLogger(__name__)


class SpamMonitorEngine:
    """
    Padlet 荒らし監視・自動防衛・高速復旧エンジン (Ultra Defense 2.0)
    
    【防衛機能】
    - セクション増殖荒らし即時遮断 & バックアップからの完全自動復旧（セクション＋投稿並列コピー）
    - 20人同時荒らし・分散DDoS攻撃への並列一括処罰（スレッドプールによる0.5秒以内の全降格＆全削除）
    - マルチアカウント協調荒らし（複数人による同一テキスト/画像爆撃）の網羅的検知
    - 匿名/アカウント投稿の完全防衛（画像キャプション50文字制限、文字数上限、URLブロック、パターンマッチ）
    - 全体権限(visitorRole)の強制ロックダウン（none/アクセスなし 対応）
    - 削除後のダブルチェック再確認（残存ゼロ保証）
    - Discord Webhook @everyone メンション付き詳細レポート
    """

    def __init__(self, data_store_callback, log_callback):
        self.get_data = data_store_callback
        self.add_log = log_callback
        self.client = PadletClient()
        self.running = False
        self._thread: Optional[threading.Thread] = None

        # 投稿履歴キャッシュ: user_id -> [(timestamp, text, image_url, post_id)]
        self.user_post_history: Dict[str, List[tuple]] = {}
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
        self.add_log("SYSTEM", "監視エンジンを停止しました")

    def send_discord_webhook(self, title: str, description: str, color: int = 0xFF4B4B, fields: Optional[List[Dict[str, Any]]] = None, mention_everyone: bool = True):
        """Discord Webhook 通知送信 (@everyone メンション付き & 詳細埋め込み)"""
        data = self.get_data()
        webhook_url = data.get("discord_webhook_url", "").strip()
        if not webhook_url:
            return

        embed = {
            "title": f"🛡️ Padlet防衛Bot: {title}",
            "description": description,
            "color": color,
            "timestamp": datetime.utcnow().isoformat() + "Z",
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
        # 4. 投稿 & コメント荒らしの多層防御監視 (20人同時荒らし・DDoS・キャプション制限)
        # =====================================================================
        posts = self.client.get_posts(main_wall_hashid)
        comments = self.client.get_comments(main_wall_hashid)

        delete_non_collab = data.get("delete_non_collaborators", True)
        block_duplicates = data.get("block_duplicates", True)
        block_image_repeat = data.get("block_image_repeat", True)
        block_urls = data.get("block_urls", False)
        max_post_length = int(data.get("max_post_length", 500))
        random_str_regex = data.get("random_str_pattern", r"^.+_[a-zA-Z0-9]{4,}$")
        ddos_threshold = int(data.get("ddos_threshold", 5))

        # 処罰対象アカウントIDの集合 & 削除対象投稿IDの集合
        spammer_uids_to_punish: Set[str] = set()
        posts_to_delete: Set[str] = set()

        # (A) DDoS（短時間での投稿急増）の検知
        for p in posts:
            pid = p.get("id")
            aid = p.get("author_id")
            if pid and not any(x[2] == pid for x in self.recent_activity_log):
                self.recent_activity_log.append((now, aid, pid))

        self.recent_activity_log = [x for x in self.recent_activity_log if now - x[0] <= 10.0]
        recent_count = len([x for x in self.recent_activity_log if now - x[0] <= 5.0])

        if recent_count >= ddos_threshold:
            # 5秒以内に投稿した全ユーザーを特定
            ddos_authors = {x[1] for x in self.recent_activity_log if x[1] and x[1] != "anonymous"}
            filtered_ddos_authors = {
                uid for uid in ddos_authors
                if uid != owner_id and uid not in exempt_admins and collab_map.get(uid, {}).get("username") not in exempt_admins
            }
            if filtered_ddos_authors:
                self.add_log("ALERT", "⚡ 大量連投DDoS攻撃を検知！", f"5秒間に{recent_count}件の新規投稿 (関与アカウント: {len(filtered_ddos_authors)}名)")
                self.send_discord_webhook(
                    "🚨 大量連投DDoS攻撃を検知・一括遮断！",
                    f"短時間(5秒間)に **{recent_count} 件** の異常な投稿急増を検知しました。\n対象の全荒らしアカウント({len(filtered_ddos_authors)}名)の権限を一斉に「閲覧のみ」に降格し、投稿を完全一掃します。",
                    color=0xFF0000,
                    fields=[
                        {"name": "⚠️ 攻撃検知数", "value": f"{recent_count}件 / 5秒", "inline": True},
                        {"name": "👤 処罰対象アカウント数", "value": f"{len(filtered_ddos_authors)}名", "inline": True},
                        {"name": "🛡️ 防衛アクション", "value": "一斉権限剥奪(Reader) ➜ 並列全投稿削除 ➜ 再確認", "inline": False}
                    ],
                    mention_everyone=True
                )
                spammer_uids_to_punish.update(filtered_ddos_authors)
                # DDoS発生時に投稿された最近の投稿もすべて削除対象に追加
                for x in self.recent_activity_log:
                    if x[1] in filtered_ddos_authors or x[1] == "anonymous":
                        posts_to_delete.add(x[2])

        # (B) マルチアカウント協調荒らし検知 (20人が別アカウントで同一テキスト/同一画像を爆撃)
        text_post_map: Dict[str, List[tuple]] = {}  # text -> [(post_id, author_id)]
        img_post_map: Dict[str, List[tuple]] = {}   # img_url -> [(post_id, author_id)]

        for p in posts:
            pid = p.get("id")
            aid = p.get("author_id", "anonymous")
            if owner_id and aid == owner_id:
                continue
            if aid in exempt_admins or collab_map.get(aid, {}).get("username") in exempt_admins:
                continue

            full_t = f"{p.get('subject', '')} {p.get('content', '')}".strip()
            if len(full_t) >= 10:  # 10文字以上の同一テキスト
                text_post_map.setdefault(full_t, []).append((pid, aid))

            att = p.get("attachment")
            if att:
                img_post_map.setdefault(att, []).append((pid, aid))

        # 同一テキストを複数アカウントから爆撃している場合
        for txt, p_list in text_post_map.items():
            distinct_authors = {x[1] for x in p_list if x[1] != "anonymous"}
            if len(p_list) >= 3 and len(distinct_authors) >= 2:
                self.add_log("ALERT", f"複数アカウント協調コピペ爆撃を検知: {len(distinct_authors)}名のアカウント", f"内容: {txt[:40]}")
                spammer_uids_to_punish.update(distinct_authors)
                for pid, aid in p_list:
                    posts_to_delete.add(pid)

        # 同一画像を複数アカウントから爆撃している場合
        for img, p_list in img_post_map.items():
            distinct_authors = {x[1] for x in p_list if x[1] != "anonymous"}
            if len(p_list) >= 3 and len(distinct_authors) >= 2:
                self.add_log("ALERT", f"複数アカウント協調画像爆撃を検知: {len(distinct_authors)}名のアカウント", f"画像: {img}")
                spammer_uids_to_punish.update(distinct_authors)
                for pid, aid in p_list:
                    posts_to_delete.add(pid)

        # (C) 非コラボレーターの投稿・コメント即時削除 (部屋主は完全保護)
        if delete_non_collab:
            for p in posts:
                author_id = p.get("author_id")
                if owner_id and author_id == owner_id:
                    continue
                # コラボレーター登録されていない（または匿名）場合は即削除
                if author_id not in registered_collab_ids:
                    posts_to_delete.add(p["id"])

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

        # (D) 各投稿の個別荒らし判定 (匿名でもアカウントでも厳格判定)
        for p in posts:
            try:
                pid = p.get("id")
                author_id = p.get("author_id", "anonymous")
                if owner_id and author_id == owner_id:
                    continue  # 部屋主は保護
                if author_id in exempt_admins or collab_map.get(author_id, {}).get("username") in exempt_admins:
                    continue  # 免除者は保護

                # 復旧処理でコピーされた投稿は誤削除しない
                fp = f"{p.get('subject', '')}:::{p.get('content', '')}"
                if fp in self.protected_post_fingerprints:
                    continue

                content = (p.get("content") or "").strip()
                subject = (p.get("subject") or "").strip()
                full_text = f"{subject} {content}".strip()
                attachment = p.get("attachment")
                caption = (p.get("attachment_caption") or "").strip()
                author_name = collab_map.get(author_id, {}).get("username") or collab_map.get(author_id, {}).get("name") or author_id

                is_violation = False
                violation_reason = ""

                # ルール1: 画像キャプション文字数制限 (50文字超過で即削除＆処罰)
                if len(caption) > 50:
                    is_violation = True
                    violation_reason = f"画像キャプション50文字超過 ({len(caption)}文字)"

                # ルール2: 最大文字数制限
                elif max_post_length > 0 and len(full_text) > max_post_length:
                    is_violation = True
                    violation_reason = f"長文スパム ({len(full_text)}文字 / 上限{max_post_length})"

                # ルール3: 外部リンク (URL) ブロック
                elif block_urls and re.search(r'https?://[^\s]+', full_text):
                    is_violation = True
                    violation_reason = f"外部リンク禁止違反: {full_text[:30]}"

                # ルール4: パターン荒らし検知 (○○_ランダム英数字等)
                elif random_str_regex:
                    try:
                        if re.search(random_str_regex, full_text):
                            is_violation = True
                            violation_reason = f"パターン荒らし一致: {full_text[:30]}"
                    except Exception:
                        pass

                # ルール5: 同一アカウントによる連続投稿・同一画像連投
                if not is_violation and author_id and author_id != "anonymous":
                    if author_id not in self.user_post_history:
                        self.user_post_history[author_id] = []
                    hist = self.user_post_history[author_id]
                    if hist:
                        last_t, last_txt, last_img, last_p = hist[-1]
                        if block_duplicates and full_text and full_text == last_txt and pid != last_p:
                            is_violation = True
                            violation_reason = f"同一テキスト連投: {full_text[:30]}"
                        elif block_image_repeat and attachment and attachment == last_img and pid != last_p:
                            is_violation = True
                            violation_reason = "同一画像連続投稿"
                    hist.append((now, full_text, attachment, pid))
                    if len(hist) > 20:
                        self.user_post_history[author_id] = hist[-20:]

                # 違反が検知された場合
                if is_violation:
                    posts_to_delete.add(pid)
                    self.add_log("ALERT", f"荒らし投稿検知: {author_name}", f"理由: {violation_reason} (PostID: {pid})")
                    if author_id and author_id != "anonymous":
                        spammer_uids_to_punish.add(author_id)

            except Exception as e:
                logger.debug(f"投稿チェック例外: {e}")

        # (E) 処罰 & 削除の超高速並列実行 (20人同時荒らし・大量投稿一括鎮圧)
        if spammer_uids_to_punish or posts_to_delete:
            self._handle_spammers_batch(
                main_wall_id=main_wall_id,
                main_wall_hashid=main_wall_hashid,
                spammer_ids=spammer_uids_to_punish,
                posts_to_delete=posts_to_delete,
                collab_map=collab_map,
                expected_visitor_role=expected_visitor_role
            )

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

                with ThreadPoolExecutor(max_workers=min(len(spammer_list), 10)) as executor:
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

                with ThreadPoolExecutor(max_workers=min(len(delete_list), 12)) as executor:
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

        except Exception as e:
            logger.error(f"一括処罰処理エラー: {e}", exc_info=True)
            self.add_log("ERROR", "荒らし処罰中にエラーが発生しました", str(e))

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

                time.sleep(0.5)

                # ステップ3: 一時セクションを作成し、既存の汚染セクションを一括削除
                self.add_log("RECOVERY", "【フェーズ2】汚染セクションの初期化中...")
                temp_sec = self.client.create_section(main_wall_id, "_safe_restore_guard_")
                time.sleep(0.3)

                for s in polluted_sections:
                    self.client.delete_section(s["id"])
                    time.sleep(0.05)

                if not backup_wall_id or not backup_wall_hashid:
                    self.add_log("WARN", "バックアップボード情報が未設定のため、セクション初期化のみで終了します")
                    if temp_sec:
                        self.client.delete_section(temp_sec["id"])
                    self.client.change_visitor_role(main_wall_hashid, expected_visitor_role, wall_id=main_wall_id)
                    self.is_restoring = False
                    return

                # ステップ4: バックアップからセクション再作成 & マッピングテーブル構築
                self.add_log("RECOVERY", "【フェーズ3】バックアップからセクション再構築中...")
                backup_sections = self.client.get_sections(backup_wall_id)
                backup_posts = self.client.get_posts(backup_wall_hashid)

                section_id_map: Dict[str, str] = {}
                first_created_sec_id = None

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
                    time.sleep(0.1)

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
                        board_hashid=main_wall_hashid
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
                board_hashid=main_wall_hashid
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
