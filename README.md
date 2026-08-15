<!-- máy chưa có tài nguyên thì tải về -->

py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

<!-- chạy dự án -->

.\.venv\Scripts\python.exe main.py
