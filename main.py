import os
import time
import base64
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / ".app-config"
(CONFIG_DIR / "Ultralytics").mkdir(parents=True, exist_ok=True)
(CONFIG_DIR / "matplotlib").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("YOLO_CONFIG_DIR", str(CONFIG_DIR))
os.environ.setdefault("MPLCONFIGDIR", str(CONFIG_DIR / "matplotlib"))

from ultralytics import YOLO

CONFIDENCE_THRESHOLD = 0.5

app = Flask(__name__, template_folder="templates")
# giới hạn kích thước tệp tải lên tối đa là 20MB
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
model = YOLO(str(BASE_DIR / "ok.pt"))


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/analyze-frame")
def analyze_frame():
    uploaded_frame = request.files.get("frame")
    if uploaded_frame is None:
        return jsonify(error="Không nhận được khung hình."), 400

    encoded = np.frombuffer(uploaded_frame.read(), dtype=np.uint8)
    frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if frame is None:
        return jsonify(error="Khung hình không hợp lệ."), 400

    started_at = time.perf_counter()
    results = model(frame, verbose=False)
    detections = []
    for result in results:
        for box in result.boxes:
            confidence = float(box.conf.item())
            if confidence < CONFIDENCE_THRESHOLD:
                continue
            x1, y1, x2, y2 = (float(value) for value in box.xyxy[0].tolist())
            detections.append({
                "class_name": model.names[int(box.cls.item())],
                "confidence": round(confidence, 4),
                "box": [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)],
            })

    has_fall = any(item["class_name"] == "fall" for item in detections)
    frame_image = None
    if has_fall:
        annotated = results[0].plot()
        max_width = 420
        if annotated.shape[1] > max_width:
            scale = max_width / annotated.shape[1]
            annotated = cv2.resize(
                annotated,
                (max_width, round(annotated.shape[0] * scale)),
                interpolation=cv2.INTER_AREA,
            )
        encoded_ok, encoded_image = cv2.imencode(
            ".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 80]
        )
        if encoded_ok:
            frame_image = "data:image/jpeg;base64," + base64.b64encode(
                encoded_image.tobytes()
            ).decode("ascii")

    return jsonify(
        has_fall=has_fall,
        detections=detections,
        frame_width=frame.shape[1],
        frame_height=frame.shape[0],
        server_time=datetime.now().strftime("%H:%M:%S %d/%m/%Y"),
        frame_image=frame_image,
        processing_ms=round((time.perf_counter() - started_at) * 1000),
    )



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
