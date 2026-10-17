# Tool Poke Đại Chiến (Protocol Automation)

Chương trình Python tự động hóa giao thức mạng 2 tầng (2-Tier Pipeline) cho game Poke Android (APK: `vn3ds_gw_250604.apk` / `com.pokedaigpguanwang.vn`).

## 🚀 Tính năng chính

- **Kiến trúc mạng 2 tầng chuẩn Reverse Engineering:**
  - **Tầng 1 (HTTP Web SDK):** Đăng ký tài khoản mới (`POST /register`), Đăng nhập Web SDK xác thực lấy User ID và SDK Token (`POST /login`), Liên kết nhân vật vào Zone (`POST /bindZone`).
  - **Tầng 2 (Raw TCP Socket):** Đóng gói header nhị phân 16-byte lồng 2 tầng CRC32, bắt tay handshake (Flag `0x0A`), xác thực socket (`seq=1`), truy vấn nhân vật (`seq=2`), tạo nhân vật (`seq=4`), truy vấn avatar/role ID (`seq=5`), nhận thưởng và gacha pet.
- **Hỗ trợ đa luồng (Multi-threading):** Chạy song song nhiều tài khoản bằng `ThreadPoolExecutor` với stagger delay ngẫu nhiên tránh nghẽn socket hoặc bị server chặn.
- **Xuất dữ liệu an toàn:** Hỗ trợ lưu trữ tài khoản, xuất báo cáo danh sách Pet gacha ra file Excel (`openpyxl`).

## 📋 Yêu cầu hệ thống

- Python 3.8+
- Cài đặt thư viện:
  ```bash
  pip install -r requirements.txt
  ```

## ⚙️ Cài đặt & Hướng dẫn sử dụng

1. **Cài đặt thư viện:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Cấu hình thông số:**
   Mở file `poke.py` và chỉnh sửa các tham số ở khối `__main__` (cuối file):
   - `MAX_WORKERS`: Số luồng chạy song song.
   - `NUM_ACCOUNTS`: Số lượng tài khoản cần tạo và xử lý.
   - `START_INDEX`: Số thứ tự tài khoản bắt đầu.
   - `USER_PREFIX`: Tiền tố tên tài khoản.
   - `PASSWORD`: Mật khẩu tài khoản.
   - `SERVER_ID`: Server ID (Mặc định: `2142`).
   - `CHIEF_BUDDY`: ID Pet khởi đầu (`1000`: Bulbasaur, `1001`: Squirtle, `1002`: Charmander).

3. **Chạy chương trình:**
   ```bash
   python poke.py
   ```

## 📖 Tài liệu kỹ thuật
Chi tiết phân tích giao thức HTTP API và TCP Socket được mô tả chi tiết tại [API-BAN-MOI.txt](API-BAN-MOI.txt).
