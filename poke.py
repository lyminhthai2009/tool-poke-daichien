# -*- coding: utf-8 -*-
"""
Chương trình Python tự động hóa giao thức Game Poke Android (APK: vn3ds_gw_250604.apk)
Hỗ trợ đầy đủ luồng 2 tầng (2-Tier Pipeline):
  1. Đăng ký tài khoản mới trên HTTP Web SDK (POST /register)
  2. Đăng nhập Web SDK lấy User ID và Token (POST /login)
  3. Mở kết nối Raw TCP Socket tới máy chủ Game (125.212.213.66:22142)
  4. Đóng gói Header nhị phân 16-byte lồng 2 tầng CRC32 chuẩn reverse engineering
  5. Gửi gói tin Handshake (Flag 0x0A) và nhận thông tin cấu hình Server
  6. Gửi gói Login Socket (seq=1) với tiền tố 'yuenan#' để nhận Session Key
  7. Gửi gói QueryAvatarAttribute (seq=2) kiểm tra nhân vật
  8. Gửi gói CreateAvatar (seq=4) tạo nhân vật mới (ví dụ: 'LOb977')
  9. Gửi gói QueryAvatarAttribute (seq=5) trích xuất Avatar UUID (avatarid) và Role ID (rid)
  10. Đồng bộ nhân vật vừa tạo về Web SDK (POST /bindZone)
"""

import sys
import os
import time
import uuid
import json
import zlib
import socket
import hashlib
import urllib.parse
from datetime import datetime
import requests
import random
import string
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Bật hiển thị UTF-8 trên Windows console, tắt QuickEdit mode và tăng độ ưu tiên tiến trình
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)
    except Exception:
        pass

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # Tắt chế độ QuickEdit của CMD để tránh bị đóng băng tiến trình khi click chuột hoặc chạy nền
        h_stdin = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
            new_mode = (mode.value & ~0x0040) | 0x0080  # Bỏ ENABLE_QUICK_EDIT_MODE, bật EXTENDED_FLAGS
            kernel32.SetConsoleMode(h_stdin, new_mode)

        # Tăng Priority lên ABOVE_NORMAL để Windows không bóp nghẽn CPU (EcoQoS/Game Mode) khi chơi Game hoặc xem YouTube
        p_handle = kernel32.GetCurrentProcess()
        kernel32.SetPriorityClass(p_handle, 0x00008000)
    except Exception:
        pass


# ==============================================================================
# KHÓA ĐỒNG BỘ ĐA LUỒNG (THREAD LOCKS) & LOGGING AN TOÀN
# ==============================================================================
excel_lock = threading.Lock()
history_lock = threading.Lock()
print_lock = threading.Lock()


def safe_print(msg: str = "", tag: str = ""):
    """In log an toàn đa luồng ra màn hình console."""
    with print_lock:
        if tag:
            print(f"[{tag}] {msg}", flush=True)
        else:
            print(msg, flush=True)


# ==============================================================================
# HẰNG SỐ HỆ THỐNG
# ==============================================================================
APP_ID = "10062"
SOURCE = "1"                                       # 1: Android
SECRET_KEY = "f43968a73df747aab4b9b1e506265050"     # SDK Secret Key
SDK_DOMAIN = "sdk-sg.pocketgamesol.com"
GAME_SERVER_IP = "125.212.213.66"                  # IP Game Server VN
GAME_SERVER_PORT = 22142                           # Port 22142 (Server ID 2142)
DEFAULT_SERVER_ID = 2142


class PokeProtocolClient:
    """Client giao thức 2 tầng cho game Poke Android."""

    def __init__(self, server_id: int = DEFAULT_SERVER_ID):
        self.server_id = server_id
        self.device_no = str(uuid.uuid4())
        self.device_model = "SM-S9180"
        self.device_os = "Android14#null#10.1.10.1"
        self.device_build = "UQ1A.240205.09031440"
        self.version = "69.0.1"
        self.sdk_version = "flt_250217"
        
        self.user_name = ""
        self.password = ""
        self.user_id = ""
        self.sdk_token = ""
        self.session_key = ""
        self.avatar_id = ""
        self.role_id = None
        self.sock = None

        # Tái sử dụng HTTP Session (Connection Pool) để tránh bắt tay SSL nhiều lần gây nghẽn mạng
        self.http = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=3)
        self.http.mount("http://", adapter)
        self.http.mount("https://", adapter)

    @staticmethod
    def md5(text: str) -> str:
        """Băm MD5 chuỗi văn bản."""
        return hashlib.md5(text.encode('utf-8')).hexdigest().lower()

    # ==========================================================================
    # TẦNG 1: HTTP API WEB SDK
    # ==========================================================================
    def register_account(self, user_name: str, raw_password: str) -> dict:
        """
        Đăng ký tài khoản SDK mới.
        Công thức Sign: sign = MD5(appId + userName + passwordHash + source + SecretKey)
        """
        self.user_name = user_name
        self.password = raw_password
        password_hash = self.md5(raw_password)
        client_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

        sign_raw = f"{APP_ID}{user_name}{password_hash}{SOURCE}{SECRET_KEY}"
        sign = self.md5(sign_raw)

        params = {
            "appId": APP_ID,
            "userName": user_name,
            "password": password_hash,
            "email": "",
            "thirdPartyId": "",
            "accountType": "0",
            "userChannel": "0",
            "advChannel": "63",
            "exInfo": "",
            "model": self.device_model,
            "operatorOs": self.device_os,
            "device": self.device_build,
            "deviceNo": self.device_no,
            "source": SOURCE,
            "clientTime": client_time,
            "version": self.version,
            "sdkVersion": self.sdk_version,
            "network": "0",
            "sign": sign
        }

        headers = {
            "Host": SDK_DOMAIN,
            "User-Agent": "Dart/3.7 (dart:io)"
        }

        url = f"https://{SDK_DOMAIN}/pocketgames/client/user/v3/register"
        resp = self.http.post(url, params=params, headers=headers, timeout=15)
        data = resp.json()

        if data.get("code") == 200:
            self.user_id = str(data["data"]["userId"])
            self.sdk_token = data.get("token", "")
            return data
        else:
            raise RuntimeError(f"Đăng ký thất bại: {data.get('error_msg')} (Code {data.get('code')})")

    def login_sdk(self, user_name: str, raw_password: str) -> dict:
        """
        Đăng nhập Web SDK.
        Công thức Sign: sign = MD5(appId + userName + passwordHash + source + SecretKey)
        """
        self.user_name = user_name
        self.password = raw_password
        password_hash = self.md5(raw_password)
        client_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

        sign_raw = f"{APP_ID}{user_name}{password_hash}{SOURCE}{SECRET_KEY}"
        sign = self.md5(sign_raw)

        params = {
            "appId": APP_ID,
            "userName": user_name,
            "password": password_hash,
            "advChannel": "63",
            "model": self.device_model,
            "operatorOs": self.device_os,
            "device": self.device_build,
            "deviceNo": self.device_no,
            "source": SOURCE,
            "clientTime": client_time,
            "version": self.version,
            "sdkVersion": self.sdk_version,
            "network": "0",
            "sign": sign
        }

        headers = {
            "Host": SDK_DOMAIN,
            "User-Agent": "Dart/3.7 (dart:io)"
        }

        url = f"https://{SDK_DOMAIN}/pocketgames/client/user/v3/login"
        resp = self.http.post(url, params=params, headers=headers, timeout=15)
        data = resp.json()

        if data.get("code") == 200:
            self.user_id = str(data["data"]["userId"])
            self.sdk_token = data.get("token", "")
            return data
        else:
            raise RuntimeError(f"Login SDK thất bại: {data.get('error_msg')}")

    def bind_zone(self, role_id: str, level: int = 1) -> dict:
        """
        Đồng bộ nhân vật về Web SDK (Mục 2.4).
        Công thức Sign chuẩn: sign = MD5(userId + appId + gameZoneId + createRole + SecretKey)
        """
        client_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        create_role = "1"
        sign_raw = f"{self.user_id}{APP_ID}{self.server_id}{create_role}{SECRET_KEY}"
        sign = self.md5(sign_raw)

        params = {
            "appId": APP_ID,
            "userId": self.user_id,
            "gameZoneId": str(self.server_id),
            "createRole": create_role,
            "advChannel": "63",
            "roleId": role_id,
            "level": str(level),
            "model": self.device_model,
            "operatorOs": self.device_os,
            "device": self.device_build,
            "deviceNo": self.device_no,
            "source": SOURCE,
            "clientTime": client_time,
            "version": self.version,
            "sdkVersion": self.sdk_version,
            "network": "0",
            "sign": sign
        }

        headers = {
            "Host": SDK_DOMAIN,
            "User-Agent": "Dart/3.7 (dart:io)"
        }

        url = f"https://{SDK_DOMAIN}/pocketgames/client/user/v3/bindZone"
        resp = self.http.post(url, params=params, headers=headers, timeout=15)
        return resp.json()

    # ==========================================================================
    # TẦNG 2: RAW TCP SOCKET GAME
    # ==========================================================================
    @staticmethod
    def pack_packet(flag: int, field_8: int = 0, field_12: int = 0, payload: bytes = b"") -> bytes:
        """
        Đóng gói 16-byte Header nhị phân lồng CRC32 (reverse engineered từ SJ::Net::Message::encode):
          - Bytes 4..6:  Tổng độ dài gói (16 + len(payload)) dạng Big-Endian
          - Byte 7:     Flag gói (0x00: Data, 0x0A: Handshake)
          - Bytes 8..11: Field 8 Big-Endian uint32
          - Bytes 12..15: Field 12 Big-Endian uint32 (Handshake = 5)
          - Bytes 2..3:  CRC32(packet[4:]) & 0xFFFF (Big-Endian uint16)
          - Bytes 0..1:  CRC32(packet[2:16]) & 0xFFFF (Big-Endian uint16)
        """
        total_len = 16 + len(payload)
        b4_7 = total_len.to_bytes(3, "big") + bytes([flag])
        b8_15 = field_8.to_bytes(4, "big") + field_12.to_bytes(4, "big")

        data_after_4 = b4_7 + b8_15 + payload
        crc1 = zlib.crc32(data_after_4) & 0xFFFF
        b2_3 = crc1.to_bytes(2, "big")

        data_2_15 = b2_3 + b4_7 + b8_15
        crc2 = zlib.crc32(data_2_15) & 0xFFFF
        b0_1 = crc2.to_bytes(2, "big")

        return b0_1 + b2_3 + b4_7 + b8_15 + payload

    def recv_one_packet(self, timeout: float = 6.0):
        """Đọc đúng 1 gói tin TCP theo Header 16-byte."""
        if not self.sock:
            return None, None
        self.sock.settimeout(timeout)
        header = b""
        while len(header) < 16:
            chunk = self.sock.recv(16 - len(header))
            if not chunk:
                return None, None
            header += chunk

        total_len = int.from_bytes(header[4:7], "big")
        payload_len = total_len - 16
        payload = b""
        while len(payload) < payload_len:
            chunk = self.sock.recv(payload_len - len(payload))
            if not chunk:
                break
            payload += chunk

        return header, payload

    @staticmethod
    def parse_payload(payload: bytes):
        """Giải nén Zlib và parse JSON."""
        if not payload:
            return None
        if payload.startswith(b"\x78\x9c") or payload.startswith(b"\x78\xda"):
            try:
                payload = zlib.decompress(payload)
            except Exception:
                pass
        
        text = payload.decode("utf-8", errors="ignore")
        if text.startswith("0;"):
            text = text[2:]
        try:
            return json.loads(text)
        except Exception:
            return text

    def build_query_body(self, json_req: list, seq: int) -> bytes:
        """Tạo chuỗi body dạng URL query string."""
        req_str = json.dumps(json_req, separators=(",", ":"))
        encoded_req = urllib.parse.quote(req_str)
        session = self.session_key if self.session_key else ""
        query = f"bv=70.0.0&request={encoded_req}&seq={seq}&session={session}&sptype=spyuenan_md&version=73.0.0"
        return query.encode("utf-8")

    def connect_socket(self):
        """Mở kết nối TCP Socket tới máy chủ."""
        port = 20000 + self.server_id
        self.sock = socket.create_connection((GAME_SERVER_IP, port), timeout=15)
        # Tắt thuật toán Nagle (TCP_NODELAY) để gửi nhận tức thì, không bị nghẽn buffer khi chơi game/xem video
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    def do_handshake(self) -> dict:
        """Gửi 16-byte Handshake (Flag 0x0A, field_12=5) và nhận thông tin server."""
        hs_packet = self.pack_packet(flag=0x0A, field_8=0, field_12=5, payload=b"")
        self.sock.sendall(hs_packet)

        h1, p1 = self.recv_one_packet(timeout=8.0)
        server_info = self.parse_payload(p1)
        h2, p2 = self.recv_one_packet(timeout=8.0)
        return server_info

    def login_socket(self) -> str:
        """Gói seq=1: Đăng nhập Socket Game. Nhận Session Key."""
        game_userid = f"yuenan#{self.user_id}"
        login_info = json.dumps({
            "appVersion": "73.0.0",
            "appBaseVersion": "70.0.0",
            "deviceId": f"DEVICE#aid{self.md5(self.device_no)[:32]}",
            "combinedDeviceId": "_",
            "deviceCheckChannel": "",
            "deviceType": 17,
            "model": self.device_model,
            "os": "Android",
            "osversion": "14",
            "mac": ""
        }, separators=(",", ":"))

        login_req = [{
            "deviceType": 2,
            "func": "Login",
            "info": login_info,
            "serverid": self.server_id,
            "userid": game_userid
        }]

        body = self.build_query_body(login_req, seq=1)
        pkt = self.pack_packet(flag=0x00, field_8=0, field_12=0, payload=body)
        self.sock.sendall(pkt)

        h, p = self.recv_one_packet(timeout=8.0)
        res = self.parse_payload(p)

        if isinstance(res, list) and len(res) > 0 and res[0].get("func") == "LoginResult":
            self.session_key = res[0].get("session_key", "")
            return self.session_key
        else:
            raise RuntimeError(f"Login Socket thất bại: {res}")

    def query_avatar_attribute(self, seq: int = 2) -> dict:
        """Gói QueryAvatarAttribute: Lấy thông tin nhân vật chi tiết."""
        req = [{"func": "QueryAvatarAttribute"}]
        body = self.build_query_body(req, seq=seq)
        pkt = self.pack_packet(flag=0x00, field_8=0, field_12=0, payload=body)
        self.sock.sendall(pkt)

        start_time = time.time()
        while (time.time() - start_time) < 6.0:
            try:
                h, p = self.recv_one_packet(timeout=1.5)
                if not p:
                    continue
                res = self.parse_payload(p)
                if isinstance(res, list) and len(res) > 0:
                    for item in res:
                        if isinstance(item, dict) and item.get("func") == "QueryAvatarAttributeResult":
                            return item
            except socket.timeout:
                continue
            except Exception:
                break
        return {}

    def create_avatar(self, avatar_name: str, chief_buddy: int = 1002, gender: int = 0) -> tuple:
        """
        Gói seq=4: Tạo nhân vật mới với tên và pet khởi đầu.
        Nếu thành công, tự động gọi tiếp QueryAvatarAttribute (seq=5) để lấy avatarid & rid.
        Trả về: (avatar_details_dict, error_code)
        """
        create_req = [{
            "avatarName": avatar_name,
            "chiefBuddy": chief_buddy,
            "func": "CreateAvatar",
            "gender": gender
        }]

        body = self.build_query_body(create_req, seq=4)
        pkt = self.pack_packet(flag=0x00, field_8=0, field_12=0, payload=body)
        self.sock.sendall(pkt)

        created_success = False
        error_code = 0

        # Lắng nghe gói CreateAvatarResult từ server
        start_time = time.time()
        while (time.time() - start_time) < 8.0:
            try:
                h, p = self.recv_one_packet(timeout=1.5)
                if not p:
                    continue
                res = self.parse_payload(p)
                if isinstance(res, list) and len(res) > 0:
                    for item in res:
                        if isinstance(item, dict):
                            func = item.get("func")
                            if func == "CreateAvatarResult":
                                error_code = item.get("error_code", 0)
                                if error_code == 0:
                                    created_success = True
                                break
                    if created_success or error_code != 0:
                        break
            except socket.timeout:
                continue
            except Exception:
                break

        avatar_details = {}
        if created_success:
            # Gửi tiếp gói seq=5 QueryAvatarAttribute để nhận đầy đủ thông tin nhân vật vừa tạo
            time.sleep(0.5)
            avatar_details = self.query_avatar_attribute(seq=5)
            self.avatar_id = avatar_details.get("avatarid", "")
            self.role_id = avatar_details.get("attribute", {}).get("rid")

        return avatar_details, error_code

    def close(self):
        """Đóng kết nối Socket & HTTP Session."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        if hasattr(self, 'http') and self.http:
            try:
                self.http.close()
            except Exception:
                pass

    def send_and_drain(self, json_req: list, seq: int, timeout: float = 4.0, expected_func: str = None, jitter: bool = True) -> list:
        """Gửi gói tin và đọc sạch các phản hồi trong socket stream."""
        if not self.sock:
            return []

        # Random delay gửi gói nhẹ (50ms - 150ms) để lưu lượng tự nhiên, tránh bị nghi ngờ spam bot
        if jitter:
            time.sleep(random.uniform(0.05, 0.15))

        body = self.build_query_body(json_req, seq=seq)
        pkt = self.pack_packet(flag=0x00, field_8=0, field_12=0, payload=body)
        self.sock.sendall(pkt)

        responses = []
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            remaining = timeout - (time.time() - start_time)
            if remaining <= 0:
                break
            try:
                chunk_timeout = min(1.0, max(0.2, remaining))
                h, p = self.recv_one_packet(timeout=chunk_timeout)
                if not p:
                    if not h:
                        break
                    continue
                res = self.parse_payload(p)
                if res:
                    if isinstance(res, list):
                        responses.extend(res)
                    else:
                        responses.append(res)
                if expected_func and any(isinstance(r, dict) and r.get("func") == expected_func for r in responses):
                    break
            except socket.timeout:
                # Không thoát vòng lặp ngay khi 1 gói bị timeout 1s, tiếp tục chờ đủ tổng timeout!
                continue
            except Exception:
                break
        return responses

    def web_login_and_claim_reward(self, raw_password: str) -> dict:
        """
        BƯỚC 1: Đăng nhập Web Sự Kiện & Nhận Quà Nâng Cấp Tài Khoản (20 Phiếu Ấp).
        - Endpoint Login: GET https://activity.pocketgamesol.com/user/sdk/login
          Công thức sign: MD5(userName + MD5(raw_password) + "v3_pokectgame_login")
        - Endpoint Nhận Thưởng: GET https://activity.pocketgamesol.com/activity/getUserReward
          groupId: 5f682ae8b5cb673c9ca740ed
          actId: 5f682ae8b5cb673c9ca740ee
          rewardId: 5f682ae8b5cb673c9ca740ef
        """
        password_hash = self.md5(raw_password)
        sign_raw = f"{self.user_name}{password_hash}v3_pokectgame_login"
        sign = self.md5(sign_raw)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
            "Origin": "https://dangcapthucung.joygame.vn",
            "Referer": "https://dangcapthucung.joygame.vn/"
        }

        # 1. Web Login
        login_url = f"https://activity.pocketgamesol.com/user/sdk/login?userName={self.user_name}&password={password_hash}&version=v3&sign={sign}"
        resp = self.http.get(login_url, headers=headers, timeout=15)
        login_res = resp.json()
        web_token = login_res.get("state", {}).get("token")

        if not web_token:
            return {"error": f"Web Login thất bại: {login_res}"}

        # 2. Lấy danh sách máy chủ & Ánh xạ đúng gameZoneId của PocketGames SDK
        sdk_zone_id = self.server_id
        try:
            zones_url = f"https://activity.pocketgamesol.com/user/sdk/zones?appId=10062&token={web_token}"
            r_z = self.http.get(zones_url, headers=headers, timeout=15).json()
            for z in r_z.get("state", []):
                if str(z.get("thirdGameZoneId")) == str(self.server_id):
                    sdk_zone_id = z.get("gameZoneId", self.server_id)
                    break
        except Exception:
            pass

        # 3. Gán nhân vật vào phiên làm việc Web Event (BẮT BUỘC: Không gọi bước này server sẽ báo 400 ERROR)
        try:
            player_url = f"https://activity.pocketgamesol.com/user/player/list?appId=10062&gameZoneId={sdk_zone_id}&token={web_token}"
            self.http.get(player_url, headers=headers, timeout=15)
        except Exception:
            pass

        # 4. Kiểm tra trạng thái thưởng (Pre-check)
        try:
            pre_check_url = f"https://activity.pocketgamesol.com/activity/getUserReward?groupId=5f682ae8b5cb673c9ca740ed&actId=5f682ae8b5cb673c9ca740ee&token={web_token}"
            self.http.get(pre_check_url, headers=headers, timeout=15)
        except Exception:
            pass

        # 5. Gửi yêu cầu nhận 20 phiếu ấp
        claim_url = (
            f"https://activity.pocketgamesol.com/activity/getUserReward"
            f"?groupId=5f682ae8b5cb673c9ca740ed"
            f"&actId=5f682ae8b5cb673c9ca740ee"
            f"&token={web_token}"
            f"&rewardId=5f682ae8b5cb673c9ca740ef"
        )
        r_claim = self.http.get(claim_url, headers=headers, timeout=15)
        claim_res = r_claim.json()
        return claim_res

    def claim_all_mailbox(self, seq_start: int = 20) -> list:
        """
        BƯỚC 2: Mở hòm thư và nhận toàn bộ quà tặng trong game (có cơ chế chờ thư đến).
        """
        seq = seq_start
        claimed_items = []
        mails_to_claim = []

        # Chờ tối đa 6 lần (tổng ~10 giây) để đảm bảo server đã gửi thư Web vào game
        for attempt in range(1, 7):
            mail_res = self.send_and_drain([{"func": "QueryMailBox"}], seq=seq, expected_func="QueryMailBoxResult", timeout=3.0)
            for item in mail_res:
                if isinstance(item, dict) and item.get("func") == "QueryMailBoxResult":
                    for m in item.get("mails", []):
                        if m.get("attachment") and m.get("status") == 0:
                            if m["id"] not in mails_to_claim:
                                mails_to_claim.append(m["id"])
            if mails_to_claim:
                break
            if attempt < 6:
                print(f"    [*] Đang đợi thư quà tặng đến hòm thư (lần {attempt}/5)...", flush=True)
                time.sleep(1.5)
            seq += 1

        for mid in mails_to_claim:
            seq += 1
            obtain_res = self.send_and_drain([{"func": "ObtainAttachment", "mailid": mid}], seq=seq, expected_func="ObtainAttachmentResult", timeout=4.0)
            for r in obtain_res:
                if isinstance(r, dict) and r.get("func") == "UpdateItem":
                    claimed_items.append(r.get("item", {}))

        return claimed_items

    def roll_lottery_x10(self, seq: int = 30) -> list:
        """
        BƯỚC 4: Rút 1 lần X10 (lotteryType: 1004).
        """
        res = self.send_and_drain([{"func": "LotteryConsume", "lotteryType": 1004}], seq=seq, timeout=5.0)
        return res

    def roll_lottery_x1(self, times: int = 10, seq_start: int = 40, min_delay: float = 0.3, max_delay: float = 0.8) -> list:
        """
        BƯỚC 5: Rút Free 1 lần + Rút X1 9 lần (lotteryType: 1003, tổng 10 lần).
        Có random delay giữa mỗi lượt quay đơn.
        """
        results = []
        for i in range(times):
            seq = seq_start + i
            res = self.send_and_drain([{"func": "LotteryConsume", "lotteryType": 1003}], seq=seq, timeout=3.0)
            results.extend(res)
            time.sleep(random.uniform(min_delay, max_delay))
        return results

    def query_pet_list(self, seq: int = 100) -> list:
        """
        BƯỚC 6: Lấy toàn bộ danh sách Pokémon mà tài khoản sở hữu.
        """
        pets = []
        for retry in range(2):
            res = self.send_and_drain([{"func": "QueryPetList"}], seq=seq + retry, expected_func="QueryPetListResult", timeout=5.0)
            for item in res:
                if isinstance(item, dict) and item.get("func") == "QueryPetListResult":
                    pets.extend(item.get("PetList", []))
            if pets:
                break
            time.sleep(1.0)
        return pets


# ==============================================================================
# HÀM XUẤT EXCEL DANH SÁCH PET GACHA (THREAD-SAFE)
# ==============================================================================
def export_pets_excel(account_info: dict, pet_list: list, file_path: str = "gacha_pets_result.xlsx"):
    """Bọc khóa excel_lock an toàn tuyệt đối khi chạy đa luồng."""
    with excel_lock:
        return _export_pets_excel_unlocked(account_info, pet_list, file_path)


def _export_pets_excel_unlocked(account_info: dict, pet_list: list, file_path: str = "gacha_pets_result.xlsx"):
    """
    Xuất danh sách Pokémon thu thập được ra file Excel (.xlsx) chuyên nghiệp.
    Tự động ghi nối tiếp (append) nếu file đã tồn tại. Đã bỏ cột Thời Gian.
    """
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = None
        if os.path.exists(file_path):
            try:
                wb = openpyxl.load_workbook(file_path)
            except Exception:
                wb = None
        if wb is None:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Danh Sách Pet Gacha"
        else:
            ws = wb.active

        # Nếu file cũ có cột 'Thời Gian' ở ô (1, 1), tự động xóa cột 1
        if ws.max_row >= 1 and ws.cell(1, 1).value == "Thời Gian":
            ws.delete_cols(1)

        # Nếu sheet mới tạo thì viết dòng tiêu đề
        if ws.max_row <= 1:
            headers = [
                "Tài Khoản", "Nhân Vật", "Server", "Role ID",
                "STT Pet", "Tên Pokémon", "Lực Chiến (LC)", "Cấp Độ", "Config ID",
                "Tư Chất (HP/Công/Thủ...)", "Tính Cách ID", "Đặc Tính ID"
            ]
            ws.append(headers)

            header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
            header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
            header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
            thin_border = Border(
                left=Side(style='thin', color='D9D9D9'),
                right=Side(style='thin', color='D9D9D9'),
                top=Side(style='thin', color='D9D9D9'),
                bottom=Side(style='thin', color='D9D9D9')
            )
            for col_idx in range(1, len(headers) + 1):
                cell = ws.cell(row=1, column=col_idx)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = header_align
                cell.border = thin_border
            ws.row_dimensions[1].height = 26

        username = account_info.get("username", "")
        char_name = account_info.get("char_name", "")
        server_id = account_info.get("server_id", "")
        role_id = account_info.get("role_id", "")

        row_fill_1 = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
        row_fill_2 = PatternFill(start_color="F2F5F9", end_color="F2F5F9", fill_type="solid")
        data_font = Font(name="Calibri", size=10)
        center_align = Alignment(horizontal="center", vertical="center")
        left_align = Alignment(horizontal="left", vertical="center")
        thin_border = Border(
            left=Side(style='thin', color='E0E0E0'),
            right=Side(style='thin', color='E0E0E0'),
            top=Side(style='thin', color='E0E0E0'),
            bottom=Side(style='thin', color='E0E0E0')
        )

        for idx, pet in enumerate(pet_list, start=1):
            pet_name = pet.get("name", "Unknown")
            fighting = pet.get("Fighting", 0)
            level = pet.get("level", 1)
            config_id = pet.get("PetConfigID", 0)
            zizhi = str(pet.get("PetBaseZiZhi", []))
            xingge = pet.get("XingGeID", 0)
            texing = str(pet.get("TeXingIdList", []))

            row_data = [
                username, char_name, server_id, role_id,
                idx, pet_name, fighting, level, config_id,
                zizhi, xingge, texing
            ]
            ws.append(row_data)
            cur_row = ws.max_row
            fill = row_fill_2 if idx % 2 == 0 else row_fill_1

            for col_idx in range(1, len(row_data) + 1):
                cell = ws.cell(row=cur_row, column=col_idx)
                cell.fill = fill
                cell.font = data_font
                cell.border = thin_border
                if col_idx in [3, 4, 5, 7, 8, 9, 11]:
                    cell.alignment = center_align
                else:
                    cell.alignment = left_align
            ws.row_dimensions[cur_row].height = 20

        for col in ws.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

        try:
            wb.save(file_path)
            safe_print(f"    [+] Đã xuất {len(pet_list)} Pokémon vào file Excel '{file_path}' thành công!", tag=username)
        except PermissionError:
            fallback = f"gacha_pets_result_{int(time.time())}_{random.randint(100, 999)}.xlsx"
            wb.save(fallback)
            safe_print(f"    [!] Cảnh báo: File '{file_path}' đang mở trong Excel. Đã lưu tạm vào '{fallback}'", tag=username)
    except Exception as e:
        safe_print(f"    [!] Lỗi xuất file Excel: {e}", tag=account_info.get("username", "Excel"))


# ==============================================================================
# HÀM TẠO TÊN NHÂN VẬT NGẪU NHIÊN (RANDOM CHARACTER NAME)
# ==============================================================================
def generate_random_char_name(length: int = 7) -> str:
    """
    Tạo tên nhân vật ngẫu nhiên gồm các ký tự từ A-Z, a-z và 0-9.
    Chọn ra 7 ký tự ngẫu nhiên và sắp xếp/xáo trộn vị trí ngẫu nhiên.
    Ví dụ: 'k8X9pZa', '9mB2wQ1'
    """
    chars_pool = string.ascii_letters + string.digits
    selected = random.choices(chars_pool, k=length)
    random.shuffle(selected)
    return "".join(selected)


# ==============================================================================
# QUY TRÌNH TOÀN DIỆN TỰ ĐỘNG HÓA 1 TÀI KHOẢN (AUTO FULL PIPELINE)
# ==============================================================================
def auto_full_pipeline(
    username: str = None,
    password: str = None,
    char_name: str = None,
    server_id: int = DEFAULT_SERVER_ID,
    chief_buddy: int = 1002,
    gender: int = 0,
    tag: str = "",
    min_delay: float = 0.5,
    max_delay: float = 1.5
):
    """
    Quy trình tự động hóa khép kín 6 giai đoạn cho 1 tài khoản (hỗ trợ đa luồng):
      GIAI ĐOẠN 1: Tạo/Xác thực tài khoản SDK & Tạo nhân vật game (nếu chưa có).
      GIAI ĐOẠN 2: Đăng nhập Web Sự Kiện & Nhận thưởng 20 vé ấp trứng (POST Web Activity).
      GIAI ĐOẠN 3: Vào game mở hòm thư & Nhận toàn bộ vật phẩm (QueryMailBox & ObtainAttachment).
      GIAI ĐOẠN 4: Rút 1 lần X10 vé cao cấp (LotteryConsume 1004).
      GIAI ĐOẠN 5: Rút Free 1 lần + 9 lần X1 (LotteryConsume 1003 x10).
      GIAI ĐOẠN 6: Lấy toàn bộ dữ liệu Pet & Lưu an toàn vào file Excel gacha_pets_result.xlsx.
    """
    def log(msg: str):
        safe_print(msg, tag=tag or username or "Auto")

    def rnd_sleep(a=min_delay, b=max_delay):
        time.sleep(random.uniform(a, b))

    log("-" * 75)
    log(f"[*] THỰC HIỆN TÀI KHOẢN: {username} | NHÂN VẬT: {char_name or 'Tự tạo ngẫu nhiên'} | SERVER: {server_id}")
    log("-" * 75)

    random_suffix = str(int(time.time() * 1000))[-4:]
    account_username = username.strip() if (username and username.strip()) else f"poke_user_{random_suffix}"
    account_password = password.strip() if (password and password.strip()) else f"ngoloan01"
    chosen_char_name = char_name.strip() if (char_name and char_name.strip()) else generate_random_char_name(7)
    server_port = 20000 + server_id

    pet_names = {1000: "Bulbasaur (Cỏ)", 1001: "Squirtle (Nước)", 1002: "Charmander (Lửa)"}

    client = PokeProtocolClient(server_id=server_id)
    seq = 1

    try:
        # ----------------------------------------------------------------------
        # BƯỚC 1: XÁC THỰC WEB SDK HOẶC ĐĂNG KÝ
        # ----------------------------------------------------------------------
        log(">>> BƯỚC 1: Đăng nhập/Đăng ký Web SDK...")
        try:
            login_sdk_res = client.login_sdk(account_username, account_password)
            log(f"    [+] Tài khoản đã tồn tại. User ID: {client.user_id}")
        except Exception:
            log(f"    [*] Đang đăng ký tài khoản mới: {account_username}...")
            reg_res = client.register_account(account_username, account_password)
            log(f"    [+] Đăng ký thành công! User ID: {client.user_id}")
            rnd_sleep(0.3, 0.8)
            client.login_sdk(account_username, account_password)

        rnd_sleep()

        # ----------------------------------------------------------------------
        # BƯỚC 2: KẾT NỐI SOCKET VÀO GAME
        # ----------------------------------------------------------------------
        log(f">>> BƯỚC 2: Kết nối Socket Game ({GAME_SERVER_IP}:{server_port})...")
        client.connect_socket()
        rnd_sleep(0.1, 0.3)
        client.do_handshake()
        rnd_sleep(0.1, 0.3)
        client.login_socket()
        log(f"    [+] Login Socket thành công! Session: {client.session_key}")
        rnd_sleep(0.3, 0.8)

        # Kiểm tra nhân vật
        seq += 1
        query_res = client.query_avatar_attribute(seq=seq)
        if query_res.get("error_code") == 1:
            log(f"    [*] Tài khoản chưa có nhân vật. Tiến hành tạo nhân vật '{chosen_char_name}'...")
            seq += 1
            avatar_info, err = client.create_avatar(avatar_name=chosen_char_name, chief_buddy=chief_buddy, gender=gender)
            if err == 2:
                alt_name = generate_random_char_name(7)
                avatar_info, err = client.create_avatar(avatar_name=alt_name, chief_buddy=chief_buddy, gender=gender)
                chosen_char_name = alt_name
            if err != 0:
                raise RuntimeError(f"Tạo nhân vật thất bại, mã lỗi: {err}")

            log(f"    [+] Tạo nhân vật thành công! Avatar UUID: {client.avatar_id} | RID: {client.role_id}")
            rnd_sleep(0.3, 0.8)
            # BindZone
            if client.avatar_id:
                client.bind_zone(role_id=client.avatar_id, level=1)
                log("    [+] Đồng bộ bindZone thành công!")
        else:
            client.avatar_id = query_res.get("avatarid", "")
            client.role_id = query_res.get("attribute", {}).get("rid")
            chosen_char_name = query_res.get("attribute", {}).get("avatarName", chosen_char_name)
            log(f"    [+] Đã tìm thấy nhân vật: {chosen_char_name} (RID: {client.role_id}, UUID: {client.avatar_id})")

        rnd_sleep()

        # ----------------------------------------------------------------------
        # BƯỚC 3: NHẬN THƯỞNG NÂNG CẤP TRÊN WEB EVENT (20 PHIẾU ẤP)
        # ----------------------------------------------------------------------
        log(">>> BƯỚC 3: Đăng nhập Web Sự Kiện & Nhận Quà Nâng Cấp (20 Phiếu Ấp)...")
        claim_res = client.web_login_and_claim_reward(raw_password=account_password)
        if claim_res.get("code") == 200:
            log(f"    [+] Nhận quà Web thành công: {claim_res.get('state', {}).get('rewardName')}")
        else:
            log(f"    [*] Trạng thái Web: {claim_res.get('state', claim_res)}")

        # Chờ ngẫu nhiên để Server Game cập nhật hòm thư
        rnd_sleep(2.0, 3.5)

        # ----------------------------------------------------------------------
        # BƯỚC 4: MỞ HÒM THƯ & NHẬN TOÀN BỘ QUÀ TẶNG
        # ----------------------------------------------------------------------
        log(">>> BƯỚC 4: Mở hòm thư ingame và nhận quà (QueryMailBox & ObtainAttachment)...")
        seq += 5
        claimed_items = client.claim_all_mailbox(seq_start=seq)
        if not claimed_items and claim_res.get("code") == 200:
            log("    [*] Chưa thấy thư vật phẩm, thử đồng bộ bindZone lại Web và kiểm tra tiếp...")
            if client.avatar_id:
                client.bind_zone(role_id=client.avatar_id, level=1)
            client.web_login_and_claim_reward(raw_password=account_password)
            rnd_sleep(2.0, 3.0)
            seq += 5
            claimed_items = client.claim_all_mailbox(seq_start=seq)

        log(f"    [+] Đã nhận quà thư! Các vật phẩm: {claimed_items if claimed_items else 'Đã nhận trước đó hoặc hòm thư trống'}")
        rnd_sleep()

        # ----------------------------------------------------------------------
        # BƯỚC 5: QUAY PET CAO CẤP (1 LẦN X10 + 1 LẦN FREE + 9 LẦN X1)
        # ----------------------------------------------------------------------
        log(">>> BƯỚC 5: Thực hiện Quay Gacha Pet...")
        seq += 10
        # Check Lottery
        client.send_and_drain([{"func": "LotteryQuery"}], seq=seq, timeout=2.0)
        rnd_sleep(0.2, 0.6)

        # Rút 1 lần X10
        seq += 1
        log("    [*] Đang quay 1 lần X10 (LotteryConsume 1004)...")
        x10_res = client.roll_lottery_x10(seq=seq)
        log("    [+] Hoàn tất Quay X10!")
        rnd_sleep(0.5, 1.0)

        # Rút 10 lần X1 (1 lần Free + 9 lần vé có random delay)
        seq += 5
        log("    [*] Đang quay 10 lần X1 (1 Free + 9 lần vé có delay ngẫu nhiên)...")
        x1_res = client.roll_lottery_x1(times=10, seq_start=seq, min_delay=0.3, max_delay=0.8)
        log("    [+] Hoàn tất 10 lượt quay đơn X1!")
        rnd_sleep()

        # ----------------------------------------------------------------------
        # BƯỚC 6: TRÍCH XUẤT DANH SÁCH PET & LƯU VÀO FILE EXCEL
        # ----------------------------------------------------------------------
        log(">>> BƯỚC 6: Trích xuất toàn bộ danh sách Pokémon & Xuất file Excel...")
        seq += 20
        pet_list = client.query_pet_list(seq=seq)
        log(f"    [+] Tổng số Pokémon sở hữu: {len(pet_list)}")

        for i, p in enumerate(pet_list, 1):
            log(f"        {i}. {p.get('name')} | LC: {p.get('Fighting')} | ConfigID: {p.get('PetConfigID')}")

        acc_summary = {
            "username": account_username,
            "char_name": chosen_char_name,
            "server_id": server_id,
            "role_id": client.role_id,
            "avatar_id": client.avatar_id
        }
        excel_file = "gacha_pets_result.xlsx"
        export_pets_excel(acc_summary, pet_list, file_path=excel_file)

        # Lưu log tài khoản an toàn đa luồng
        result_record = {
            "created_at": datetime.now().isoformat(),
            "server_id": server_id,
            "account": {
                "username": account_username,
                "password": account_password,
                "user_id": client.user_id,
                "sdk_token": client.sdk_token
            },
            "character": {
                "avatar_name": chosen_char_name,
                "role_id": client.role_id,
                "avatar_id": client.avatar_id,
            },
            "pets_count": len(pet_list)
        }

        with history_lock:
            with open("last_created_account.json", "w", encoding="utf-8") as f:
                json.dump(result_record, f, ensure_ascii=False, indent=2)

            history_line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] User: {account_username} | Pass: {account_password} | Server: {server_id} | Char: {chosen_char_name} | Pets: {len(pet_list)}\n"
            with open("accounts_history.txt", "a", encoding="utf-8") as f:
                f.write(history_line)

        log(f"    [+] Hoàn tất tài khoản {account_username} ({len(pet_list)} Pets)!")

    except Exception as e:
        log(f"\n[!] LỖI TRONG QUÁ TRÌNH THỰC THI: {e}")
        import traceback
        with print_lock:
            traceback.print_exc()
    finally:
        client.close()


# ==============================================================================
# HÀM CHẠY HÀNG LOẠT ĐA LUỒNG (MULTI-THREADED BATCH RUNNER)
# ==============================================================================
def batch_run_pipeline(
    user_prefix: str = "yogurtpoke",
    char_prefix: str = "",
    start_index: int = 94,
    num_accounts: int = 10,
    max_workers: int = 3,
    min_delay: float = 0.5,
    max_delay: float = 1.5,
    password: str = "ngoloan01",
    server_id: int = DEFAULT_SERVER_ID,
    chief_buddy: int = 1002,
    gender: int = 0
):
    """
    Chạy đa luồng song song tự động tạo nhiều tài khoản cùng lúc.
    Mỗi luồng phụ trách 1 tài khoản độc lập.
    Hỗ trợ random delay thao tác và random gửi gói tin socket.
    """
    char_display = f"{char_prefix}XXX (3 chữ số)" if (char_prefix and char_prefix.strip()) else "Ngẫu nhiên 7 ký tự (A-Z, a-z, 0-9)"
    safe_print("=" * 80)
    safe_print(f"🚀 BẮT ĐẦU CHẠY ĐA LUỒNG TẠO {num_accounts} TÀI KHOẢN ({max_workers} LUỒNG SONG SONG)")
    safe_print(f"   - Từ tài khoản : {user_prefix}{start_index:04d} -> {user_prefix}{start_index + num_accounts - 1:04d}")
    safe_print(f"   - Số luồng     : {max_workers} luồng đồng thời (Mỗi luồng 1 acc)")
    safe_print(f"   - Random Delay : {min_delay}s -> {max_delay}s (gửi gói & thao tác)")
    safe_print(f"   - Tiền tố Char : {char_display}")
    safe_print(f"   - Mật khẩu     : {password}")
    safe_print(f"   - Server ID    : {server_id}")
    safe_print("=" * 80)

    # Chuẩn bị danh sách tài khoản
    tasks = []
    for i in range(start_index, start_index + num_accounts):
        username = f"{user_prefix}{i:04d}"
        if char_prefix and char_prefix.strip():
            char_name = f"{char_prefix.strip()}{i:03d}"
        else:
            char_name = generate_random_char_name(7)
        idx_count = i - start_index + 1
        tasks.append((idx_count, i, username, char_name))

    def run_worker_task(task_args):
        idx_count, actual_idx, username, char_name = task_args
        tag = f"Acc-{actual_idx:04d}|{username}"

        # Stagger khởi đầu nhẹ ngẫu nhiên để các luồng không gửi gói trùng 1 microsecond
        stagger = random.uniform(0.1, 1.2)
        time.sleep(stagger)

        safe_print(f"🔥 BẮT ĐẦU [{idx_count}/{num_accounts}]: USER = {username} | CHAR = {char_name}", tag=tag)
        try:
            auto_full_pipeline(
                username=username,
                password=password,
                char_name=char_name,
                server_id=server_id,
                chief_buddy=chief_buddy,
                gender=gender,
                tag=tag,
                min_delay=min_delay,
                max_delay=max_delay
            )
            return True, username
        except Exception as e:
            safe_print(f"[!] Lỗi khi xử lý {username}: {e}", tag=tag)
            return False, username

    if max_workers <= 1:
        # Chế độ 1 luồng tuần tự
        for t in tasks:
            run_worker_task(t)
    else:
        # Chế độ đa luồng song song
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(run_worker_task, t): t for t in tasks}
            completed = 0
            for future in as_completed(futures):
                completed += 1
                success, uname = future.result()
                status_str = "THÀNH CÔNG" if success else "THẤT BẠI"
                safe_print(f"[*] Tiến độ chung: [{completed}/{num_accounts}] - {uname} {status_str}", tag="TIẾN ĐỘ")

    safe_print("\n" + "=" * 80)
    safe_print(f"🎉 HOÀN THÀNH TẤT CẢ {num_accounts} TÀI KHOẢN QUA {max_workers} LUỒNG!")
    safe_print(f"   - Danh sách Pet thu thập được đã lưu an toàn tại: gacha_pets_result.xlsx")
    safe_print("=" * 80)


# ==============================================================================
# ĐIỂM CHẠY CHÍNH (MAIN) - BẠN CHỈNH SỬA THÔNG TIN VÀ SỐ LUỒNG TẠI ĐÂY:
# ==============================================================================
if __name__ == "__main__":
    # ==========================================================================
    # ⚙️ CẤU HÌNH ĐA LUỒNG & RANDOM DELAY (MULTI-THREADING & RANDOM JITTER)
    # ==========================================================================
    MAX_WORKERS   = 6                 # SỐ LUỒNG CHẠY SONG SONG (Ví dụ: 2, 3, 5 luồng. Mỗi luồng 1 acc cùng lúc)
    NUM_ACCOUNTS  = 500                # TỔNG SỐ LƯỢNG TÀI KHOẢN CẦN LÀM
    START_INDEX   = 300                # SỐ THỨ TỰ BẮT ĐẦU (Ví dụ: 96 -> yogurtpoke0096)
    USER_PREFIX   = "yogurtpoke"      # TIỀN TỐ TÊN TÀI KHOẢN (-> yogurtpoke0094, yogurtpoke0095...)
    CHAR_PREFIX   = ""                # TIỀN TỐ TÊN NHÂN VẬT (Để trống "" -> Tự sinh ngẫu nhiên 7 ký tự A-Z, a-z, 0-9)
    PASSWORD      = "ngoloan01"       # MẬT KHẨU CHUNG
    SERVER_ID     = 2142              # MÁY CHỦ GAME MUỐN VÀO (Mặc định: 2142)
    CHIEF_BUDDY   = 1002              # PET CHỌN: 1000 (Bulbasaur/Cỏ), 1001 (Squirtle/Nước), 1002 (Charmander/Lửa)
    GENDER        = 0                 # GIỚI TÍNH: 0 = Nam, 1 = Nữ

    # CẤU HÌNH RANDOM DELAY (ĐỘ TRỄ NGẪU NHIÊN TRÁNH BỊ SERVER CHẶN)
    MIN_DELAY     = 0.5               # Độ trễ ngẫu nhiên tối thiểu (giây) giữa các thao tác
    MAX_DELAY     = 1.5               # Độ trễ ngẫu nhiên tối đa (giây) giữa các thao tác

    batch_run_pipeline(
        user_prefix=USER_PREFIX,
        char_prefix=CHAR_PREFIX,
        start_index=START_INDEX,
        num_accounts=NUM_ACCOUNTS,
        max_workers=MAX_WORKERS,
        min_delay=MIN_DELAY,
        max_delay=MAX_DELAY,
        password=PASSWORD,
        server_id=SERVER_ID,
        chief_buddy=CHIEF_BUDDY,
        gender=GENDER
    )

