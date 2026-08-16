<!-- máy chưa có tài nguyên thì tải về -->

py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

<!-- chạy dự án -->

.\.venv\Scripts\python.exe main.py

<!-- cài sqlite view -->

.\.venv\Scripts\python.exe -m pip install sqlite-web

<!-- mở lên coi -->

.\.venv\Scripts\sqlite_web.exe -p 8080 -H 127.0.0.1 db\camerax.db
