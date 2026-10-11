import re
import time
import requests
from typing import Optional, Dict, List, Any
from urllib.parse import quote
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context


class TLSAdapter(HTTPAdapter):
    """PadletのCloudflare / TLSフィンガープリント対策アダプタ"""
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.load_default_certs()
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


class PadletClient:
    """Padlet 完全内部API専用クライアント (公式有料APIは完全不使用・完全無料稼働)"""

    def __init__(self, api_key: str = "", session_token: str = "", secondary_session_token: str = ""):
        # 有料APIキーは完全撤廃。トークンがセッショントークンとして渡された場合も自動吸収
        tokens = [t.strip() for t in [session_token, secondary_session_token, api_key] if t and t.strip()]
        self.session_tokens = tokens
        self._token_index = 0
        self.session = requests.Session()
        self.session.mount('https://', TLSAdapter())
        self._next_allowed = 0.0
        self.owner_id: Optional[str] = None

    def set_credentials(self, api_key: str = "", session_token: str = "", secondary_session_token: str = ""):
        tokens = [t.strip() for t in [session_token, secondary_session_token, api_key] if t and t.strip()]
        if tokens:
            self.session_tokens = tokens

    def _get_active_session_token(self) -> str:
        """2つのアカウントトークンをローテーションしてレート制限を分散"""
        if not self.session_tokens:
            return ""
        token = self.session_tokens[self._token_index % len(self.session_tokens)]
        self._token_index += 1
        return token

    def _get_internal_headers(self) -> Dict[str, str]:
        """内部 API (padlet.com) 用ヘッダー (アカウント交互ローテーション & ブラウザ完全模倣)"""
        token = self._get_active_session_token()
        raw_token = token.replace("Bearer ", "").strip()
        return {
            'Authorization': f"Bearer {raw_token}" if raw_token else "",
            'Accept': 'application/json, application/vnd.api+json',
            'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Origin': 'https://padlet.com',
            'Referer': 'https://padlet.com/',
            'Sec-Ch-Ua': '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin'
        }

    def _handle_rate_limit(self, response: requests.Response):
        """レート制限ヘッダーに応じて待機時刻を設定"""
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            wait_time = float(retry_after) if retry_after else 2.0
            self._next_allowed = time.time() + wait_time
            return

        remaining = response.headers.get("X-RateLimit-Remaining")
        reset_time = response.headers.get("X-RateLimit-Reset")
        if remaining is not None:
            try:
                if int(remaining) <= 3:
                    if reset_time:
                        target = float(reset_time) + 0.2
                        if target > time.time():
                            self._next_allowed = target
                    else:
                        self._next_allowed = time.time() + 0.5
            except ValueError:
                pass

    def request(self, method: str, url: str, is_official: bool = False, **kwargs) -> requests.Response:
        """内部APIレートリミットを考慮した安全・高速なHTTPリクエスト実行"""
        wait = self._next_allowed - time.time()
        if wait > 0:
            time.sleep(wait)

        headers = self._get_internal_headers()
        if 'headers' in kwargs:
            headers.update(kwargs.pop('headers'))

        timeout = kwargs.pop('timeout', 12)
        response = self.session.request(method, url, headers=headers, timeout=timeout, **kwargs)
        self._handle_rate_limit(response)
        return response

    # -------------------------------------------------------------
    # ボード URL 自動解決
    # -------------------------------------------------------------
    def resolve_board(self, input_str: str) -> Optional[Dict[str, Any]]:
        """
        PadletのURL (例: https://padlet.com/username/xxxxxx) または 数値ID または slug から、
        wall_id (数値), wall_hashid (英数字文字列), タイトル, 背景画像を取得・解決する。
        """
        if not input_str:
            return None
        raw = str(input_str).strip()
        clean_url = raw.split('?')[0].split('#')[0].rstrip('/')

        wall_id = None
        title = None
        wall_hashid = None
        attrs = {}

        # 1. すでに数値IDの場合
        if clean_url.isdigit():
            try:
                wid = int(clean_url)
                res = self.request('GET', f"https://padlet.com/api/5/walls/{wid}")
                if res.status_code == 200:
                    attrs = res.json().get("data", {}).get("attributes", {})
                    wall_id = wid
                    wall_hashid = attrs.get("hashid")
                    title = attrs.get("title")
            except Exception:
                pass

        # 2. links API による解決
        if not wall_id and ("padlet.com" in clean_url or "/" in clean_url):
            target_url = clean_url if clean_url.startswith("http") else f"https://{clean_url}"
            encoded = quote(target_url, safe="")
            link_api = f"https://padlet.com/api/1/links?url={encoded}"
            try:
                res = self.request('GET', link_api)
                if res.status_code == 200:
                    ldata = res.json().get("data", {})
                    lid = str(ldata.get("id", ""))
                    m = re.search(r'\d+', lid)
                    if m:
                        wall_id = int(m.group(0))
                    lattrs = ldata.get("attributes", {})
                    if lattrs.get("title"):
                        title = lattrs.get("title")
            except Exception:
                pass

        # 3. HTML直接スクレイピングフォールバック
        if not wall_id and ("padlet.com" in clean_url or "/" in clean_url):
            target_url = clean_url if clean_url.startswith("http") else f"https://{clean_url}"
            try:
                res = self.session.get(target_url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }, timeout=10)
                if res.status_code == 200:
                    html_text = res.text
                    m = re.search(r'wallId["\'\:\s]+([0-9]+)', html_text)
                    if m:
                        wall_id = int(m.group(1))
                    mt = re.search(r'<title>(.*?)</title>', html_text, re.IGNORECASE)
                    if mt:
                        title = mt.group(1).replace(" - Padlet", "").strip()
            except Exception:
                pass

        # wall_id が取得できた場合、/api/5/walls/{wall_id} で最新情報を補完
        if wall_id:
            if not wall_hashid or not title:
                try:
                    res = self.request('GET', f"https://padlet.com/api/5/walls/{wall_id}")
                    if res.status_code == 200:
                        attrs = res.json().get("data", {}).get("attributes", {})
                        wall_hashid = attrs.get("hashid")
                        title = title or attrs.get("title")
                except Exception:
                    pass

            bg = attrs.get("background") or {}
            bg_url = bg.get("url") if isinstance(bg, dict) else None
            owner = attrs.get("builder") or attrs.get("owner") or {}
            owner_id = str(owner.get("id", "")) if isinstance(owner, dict) and owner.get("id") else None
            if not owner_id and attrs.get("owner_id"):
                owner_id = str(attrs.get("owner_id"))
            if owner_id:
                self.owner_id = owner_id

            return {
                "wall_id": wall_id,
                "wall_hashid": wall_hashid,
                "title": title or f"Padlet ({wall_id})",
                "background_url": bg_url,
                "address": attrs.get("address", ""),
                "owner_id": owner_id
            }

        return None

    def get_access_settings(self, wall_hashid: str) -> Dict[str, Any]:
        """部屋全体のアクセス権限 (visitorRole, password, requireLogin 等) を取得"""
        try:
            res = self.request('GET', f"https://padlet.com/api/10/walls/{wall_hashid}/access-settings")
            if res.status_code == 200:
                attrs = res.json().get("data", {}).get("attributes", {})
                return attrs
        except Exception:
            pass
        return {}

    def detect_custom_properties(self, wall_hashid: str) -> tuple[Optional[str], Optional[str]]:
        """
        最新の投稿を取得し、既存の投稿に投稿フィールド(custom_properties)がある場合は、
        カスタムキーとカスタム値を自動検出して返す。
        """
        if not wall_hashid:
            return None, None
        try:
            res = self.request('GET', f"https://padlet.com/api/10/wishes?wall_hashid={wall_hashid}&page=1&limit=50")
            if res.status_code == 200:
                wishes = res.json().get("data", [])
                for w in wishes:
                    cp = w.get("attributes", {}).get("custom_properties")
                    if cp and isinstance(cp, dict):
                        for k, v in cp.items():
                            if k and str(v).strip():
                                return str(k).strip(), str(v).strip()
        except Exception:
            pass
        return None, None

    # -------------------------------------------------------------
    # 投稿 (Wishes) 関連 API
    # -------------------------------------------------------------
    def get_posts(self, wall_hashid: str, limit: int = 100) -> List[Dict[str, Any]]:
        """全投稿を取得 (custom_properties等の詳細属性も完全抽出)"""
        posts = []
        page = 1
        while page <= 10:  # 最大1000件まで探索
            try:
                res = self.request('GET', f"https://padlet.com/api/10/wishes?wall_hashid={wall_hashid}&page={page}&limit={limit}")
                if res.status_code != 200:
                    break
                data = res.json().get("data", [])
                if not data:
                    break

                for item in data:
                    attrs = item.get("attributes", {})
                    # 件名・本文抽出
                    subject_val = attrs.get("subject", "")
                    subject_text = str(subject_val or "")

                    body_val = attrs.get("body")
                    content_text = ""
                    if isinstance(body_val, dict):
                        content_text = body_val.get("text", "") or ""
                    elif isinstance(body_val, str):
                        content_text = body_val
                    elif body_val is not None:
                        content_text = str(body_val)
                    
                    attachment_data = attrs.get("attachment")
                    attachment_url = None
                    attachment_caption = ""
                    if isinstance(attachment_data, dict):
                        attachment_url = attachment_data.get("url")
                        attachment_caption = str(attachment_data.get("caption", "") or "")
                    elif isinstance(attachment_data, str):
                        attachment_url = attachment_data

                    # Padletの仕様に応じたcaption属性のフォールバック
                    if not attachment_caption and attrs.get("caption"):
                        attachment_caption = str(attrs.get("caption") or "")

                    author_id = attrs.get("author_id")
                    custom_props = attrs.get("custom_properties")

                    posts.append({
                        "id": str(item.get("id")),
                        "subject": subject_text,
                        "content": content_text,
                        "author_id": str(author_id) if author_id is not None else "anonymous",
                        "attachment": attachment_url,
                        "attachment_caption": attachment_caption.strip(),
                        "section_id": str(attrs.get("wall_section_id", "")) if attrs.get("wall_section_id") else None,
                        "custom_properties": custom_props if isinstance(custom_props, dict) else None,
                        "created_at": attrs.get("created_at")
                    })

                if len(data) < limit:
                    break
                page += 1
            except Exception:
                break

        return posts

    def create_post(
        self,
        wall_id: int,
        subject: str = "",
        body: str = "",
        section_id: Optional[str] = None,
        attachment: Optional[str] = None,
        board_hashid: Optional[str] = None,
        custom_key: Optional[str] = None,
        custom_value: Optional[str] = None,
        custom_properties: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        投稿作成 (完全内部API: POST https://padlet.com/api/5/wishes)
        有料APIは完全不使用。カスタム投稿フィールド(custom_properties)にも完全対応。
        """
        wall_id_num = int(wall_id) if str(wall_id).isdigit() else wall_id
        sec_num = int(section_id) if (section_id and str(section_id).isdigit()) else section_id

        # カスタムフィールド辞書の構築
        cp_dict: Dict[str, Any] = {}
        if custom_properties and isinstance(custom_properties, dict):
            cp_dict.update(custom_properties)
        if custom_key and str(custom_key).strip():
            cp_dict[str(custom_key).strip()] = str(custom_value or "").strip()

        # パターン1: bodyがプレーンテキスト + wall_section_id (標準形式)
        payload1: Dict[str, Any] = {
            "wallId": wall_id_num,
            "wall_id": wall_id_num,
            "subject": subject or "",
            "body": body or ""
        }
        if sec_num is not None:
            payload1["wallSectionId"] = sec_num
            payload1["wall_section_id"] = sec_num
        if cp_dict:
            payload1["custom_properties"] = cp_dict
        if attachment:
            payload1["attachment"] = attachment
            payload1["wish_content"] = {
                "attachment_props": {"url": attachment},
                "is_processed": False
            }

        url1 = "https://padlet.com/api/5/wishes"
        if sec_num is not None:
            url1 = f"https://padlet.com/api/5/wishes?wall_id={wall_id_num}&wall_section_id={sec_num}"

        try:
            res = self.request('POST', url1, json=payload1, timeout=8)
            if res.status_code in (200, 201):
                return True
        except Exception:
            pass

        # パターン2: bodyが {"text": body} 形式
        payload2 = dict(payload1)
        payload2["body"] = {"text": body or ""}
        try:
            res = self.request('POST', "https://padlet.com/api/5/wishes", json=payload2, timeout=8)
            if res.status_code in (200, 201):
                return True
        except Exception:
            pass

        # パターン3: 単純直下POST
        try:
            res = self.request('POST', f"https://padlet.com/api/5/wishes?wall_id={wall_id_num}", json=payload1, timeout=8)
            if res.status_code in (200, 201):
                return True
        except Exception:
            pass

        return False

    def delete_post(self, post_id: str) -> bool:
        """投稿削除"""
        try:
            res = self.request('DELETE', f"https://padlet.com/api/5/wishes/{post_id}")
            return res.status_code in (200, 204)
        except Exception:
            return False

    # -------------------------------------------------------------
    # コメント 関連 API
    # -------------------------------------------------------------
    def get_comments(self, wall_hashid: str) -> List[Dict[str, Any]]:
        """全コメント取得"""
        comments = []
        page = 1
        while page <= 10:
            try:
                res = self.request('GET', f"https://padlet.com/api/9/comments?wall_hashid={wall_hashid}&page={page}&limit=100")
                if res.status_code != 200:
                    break
                data = res.json().get("data", [])
                if not data:
                    break
                comments.extend(data)
                if len(data) < 100:
                    break
                page += 1
            except Exception:
                break
        return comments

    def delete_comment(self, comment_id: str) -> bool:
        """コメント削除"""
        for ep in [f"https://padlet.com/api/8/comments/{comment_id}", f"https://padlet.com/api/9/comments/{comment_id}"]:
            try:
                res = self.request('DELETE', ep)
                if res.status_code in (200, 204):
                    return True
            except Exception:
                continue
        return False

    # -------------------------------------------------------------
    # セクション 関連 API
    # -------------------------------------------------------------
    def get_sections(self, wall_id: int) -> List[Dict[str, Any]]:
        """セクション一覧取得"""
        try:
            res = self.request('GET', f"https://padlet.com/api/5/wall_sections?wall_id={wall_id}")
            if res.status_code != 200:
                return []
            sections = []
            for s in res.json().get("data", []):
                attrs = s.get("attributes", {})
                sections.append({
                    "id": str(s.get("id")),
                    "hashid": attrs.get("hashid"),
                    "title": attrs.get("title") or attrs.get("name", "名称未設定"),
                    "sort_index": attrs.get("sort_index")
                })
            sections.sort(key=lambda s: (s.get("sort_index") is None, s.get("sort_index")))
            return sections
        except Exception:
            return []

    def create_section(self, wall_id: int, title: str) -> Optional[Dict[str, Any]]:
        """新規セクション作成"""
        payload = {
            "wall_id": wall_id,
            "title": title,
            "sort_index": time.time(),
            "gap_size": None,
            "color": None
        }
        try:
            res = self.request('POST', "https://padlet.com/api/5/wall_sections", json=payload)
            if res.status_code in (200, 201):
                data = res.json().get("data", {})
                return {
                    "id": str(data.get("id")),
                    "title": title
                }
        except Exception:
            pass
        return None

    def delete_section(self, section_id: str) -> bool:
        """セクション削除"""
        try:
            res = self.request('DELETE', f"https://padlet.com/api/5/wall_sections/{section_id}?soft_delete=true")
            return res.status_code in (200, 204)
        except Exception:
            return False

    # -------------------------------------------------------------
    # コラボレーター / 権限 関連 API
    # -------------------------------------------------------------
    def get_collaborators(self, wall_hashid: str, wall_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """コラボレーター一覧取得"""
        collabs = []
        # 方法1: access-settings (wall_hashid)
        if wall_hashid:
            try:
                res = self.request('GET', f"https://padlet.com/api/10/walls/{wall_hashid}/access-settings")
                if res.status_code == 200:
                    items = res.json().get("data", {}).get("attributes", {}).get("collaborators", [])
                    for c in items:
                        collabs.append({
                            "user_id": str(c.get("id")),
                            "hashid": c.get("hashid"),
                            "name": c.get("name") or c.get("username", "Unknown"),
                            "username": c.get("username"),
                            "role": c.get("role")
                        })
                    if collabs:
                        return collabs
            except Exception:
                pass

        # 方法2: /api/8/walls/{wall_id}/collaborators
        if wall_id:
            try:
                res = self.request('GET', f"https://padlet.com/api/8/walls/{wall_id}/collaborators")
                if res.status_code == 200:
                    for c in res.json().get("data", []):
                        collabs.append({
                            "user_id": str(c.get("id")),
                            "name": c.get("name") or "Unknown",
                            "role": c.get("role")
                        })
            except Exception:
                pass

        return collabs

    def change_user_role(self, wall_id: int, user_id: str, role: str = "reader", board_hashid: Optional[str] = None) -> bool:
        """
        荒らしアカウントをコラボレーターから削除せず、確実に「読者 (reader)」に設定・固定する。
        既存コラボレーターの権限変更(PATCH)および未登録ユーザーの読者追加(POST)の両方に対応。
        """
        if not user_id or user_id == "anonymous":
            return False

        # 1. 既存コラボレーターの権限変更 (PATCH) - 読者に降格
        patch_urls = [
            f"https://padlet.com/api/8/walls/{wall_id}/user-collaborators/{user_id}",
            f"https://padlet.com/api/10/walls/{wall_id}/user-collaborators/{user_id}"
        ]
        for url in patch_urls:
            try:
                res = self.request('PATCH', url, json={"role": role}, timeout=5)
                if res.status_code in (200, 204):
                    return True
            except Exception:
                pass

        # 2. まだコラボレーターにいない場合は、読者(reader)としてコラボレーターに追加・登録 (POST)
        # コラボレーターから削除するのではなく、読者としてコラボレーター内に縛り付けて投稿を物理的に封殺
        post_urls = [
            f"https://padlet.com/api/8/walls/{wall_id}/user-collaborators",
            f"https://padlet.com/api/10/walls/{wall_id}/user-collaborators"
        ]
        post_payloads = [
            {"user_id": user_id, "role": role},
            {"userId": user_id, "role": role},
            {"user_id": int(user_id) if str(user_id).isdigit() else user_id, "role": role}
        ]
        for url in post_urls:
            for p in post_payloads:
                try:
                    res = self.request('POST', url, json=p, timeout=5)
                    if res.status_code in (200, 201, 204):
                        return True
                except Exception:
                    pass
        return False

    def change_visitor_role(self, wall_hashid: str, role: str = "reader", wall_id: Optional[int] = None) -> bool:
        """訪問者のデフォルト権限変更 (即座に確実に反映させるマルチペイロード・マルチエンドポイント対応)"""
        # Padletのバージョン差異に対応するペイロード候補
        payloads = [
            {"visitorRole": role},
            {"data": {"attributes": {"visitorRole": role}}},
            {"visitor_role": role}
        ]
        
        endpoints = [f"https://padlet.com/api/10/walls/{wall_hashid}/access-settings"]
        if wall_id:
            endpoints.append(f"https://padlet.com/api/10/walls/{wall_id}/access-settings")
            endpoints.append(f"https://padlet.com/api/8/walls/{wall_id}/access-settings")

        for ep in endpoints:
            for p in payloads:
                try:
                    res = self.request('PATCH', ep, json=p, timeout=5)
                    if res.status_code in (200, 204):
                        return True
                except Exception:
                    continue
        return False
