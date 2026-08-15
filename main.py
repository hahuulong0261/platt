import os
import time
import base64
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from uuid import UUID, uuid4

import cv2
import numpy as np
from flask import Flask, jsonify, render_template, request, url_for
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / ".app-config"
(CONFIG_DIR / "Ultralytics").mkdir(parents=True, exist_ok=True)
(CONFIG_DIR / "matplotlib").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("YOLO_CONFIG_DIR", str(CONFIG_DIR))
os.environ.setdefault("MPLCONFIGDIR", str(CONFIG_DIR / "matplotlib"))

from ultralytics import YOLO

UPLOAD_DIR = BASE_DIR / "uploads"
RESULT_DIR = BASE_DIR / "static" / "results"
CONFIDENCE_THRESHOLD = 0.5
MAX_EVIDENCE_IMAGES = 12

app = Flask(__name__, template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
model = YOLO(str(BASE_DIR / "ok.pt"))
active_jobs = {}
active_jobs_lock = Lock()


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


@app.post("/analyze")
def analyze_video():
    uploaded_file = request.files.get("file")
    if uploaded_file is None or not uploaded_file.filename:
        return jsonify(error="Vui lòng chọn một video MP4."), 400
    safe_name = secure_filename(uploaded_file.filename)
    if Path(safe_name).suffix.lower() != ".mp4":
        return jsonify(error="Chỉ hỗ trợ tệp MP4."), 400

    requested_job_id = request.form.get("job_id")
    try:
        job_id = UUID(requested_job_id).hex if requested_job_id else uuid4().hex
    except ValueError:
        return jsonify(error="Mã lượt kiểm tra không hợp lệ."), 400
    with active_jobs_lock:
        cancel_event = active_jobs.get(job_id)
        if cancel_event is None:
            cancel_event = Event()
            active_jobs[job_id] = cancel_event
    upload_path = UPLOAD_DIR / f"{job_id}.mp4"
    evidence_dir = RESULT_DIR / job_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    uploaded_file.save(upload_path)
    capture = cv2.VideoCapture(str(upload_path))
    if not capture.isOpened():
        upload_path.unlink(missing_ok=True)
        return jsonify(error="Không thể đọc video. Hãy thử một tệp MP4 khác."), 400

    fps = capture.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0:
        fps = 25.0
    total_frames = 0
    fall_frames = 0
    first_fall_frame = None
    max_confidence = 0.0
    evidence = []
    try:
        while True:
            if cancel_event.is_set():
                return jsonify(cancelled=True, message="Đã hủy kiểm tra video."), 409
            ok, frame = capture.read()
            if not ok:
                break
            frame_index = total_frames
            total_frames += 1

            # Preserve the original model, threshold and exact `fall` class rule.
            results = model(frame, verbose=False)
            fall_confidences = []
            for result in results:
                for box in result.boxes:
                    confidence = float(box.conf.item())
                    if confidence < CONFIDENCE_THRESHOLD:
                        continue
                    if model.names[int(box.cls.item())] == "fall":
                        fall_confidences.append(confidence)
            if not fall_confidences:
                continue

            fall_frames += 1
            frame_confidence = max(fall_confidences)
            max_confidence = max(max_confidence, frame_confidence)
            if first_fall_frame is None:
                first_fall_frame = frame_index
            if len(evidence) < MAX_EVIDENCE_IMAGES:
                image_name = f"frame-{frame_index:08d}.jpg"
                cv2.imwrite(str(evidence_dir / image_name), results[0].plot())
                evidence.append({
                    "frame": frame_index,
                    "time_seconds": round(frame_index / fps, 2),
                    "confidence": round(frame_confidence, 4),
                    "image_url": url_for("static", filename=f"results/{job_id}/{image_name}"),
                })
    finally:
        capture.release()
        upload_path.unlink(missing_ok=True)
        with active_jobs_lock:
            active_jobs.pop(job_id, None)

    if total_frames == 0:
        return jsonify(error="Video không chứa khung hình có thể đọc được."), 400
    return jsonify(
        has_fall=fall_frames > 0,
        total_frames=total_frames,
        fall_frames=fall_frames,
        fall_ratio=round(fall_frames / total_frames * 100, 2),
        first_fall_time=round(first_fall_frame / fps, 2) if first_fall_frame is not None else None,
        max_confidence=round(max_confidence, 4) if fall_frames else None,
        evidence=evidence,
    )


@app.post("/cancel/<job_id>")
def cancel_analysis(job_id):
    try:
        job_id = UUID(job_id).hex
    except ValueError:
        return jsonify(error="Mã lượt kiểm tra không hợp lệ."), 400
    with active_jobs_lock:
        cancel_event = active_jobs.get(job_id)
        if cancel_event is None:
            cancel_event = Event()
            active_jobs[job_id] = cancel_event
    cancel_event.set()
    return jsonify(message="Đang hủy kiểm tra…")


@app.errorhandler(413)
def file_too_large(_error):
    return jsonify(error="Video vượt quá giới hạn 500 MB."), 413


if __name__ == "__main__":
    UPLOAD_DIR.mkdir(exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=False)
