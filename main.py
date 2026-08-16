import os
import time
import base64
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock
from uuid import uuid4

import cv2
import firebase_admin
import numpy as np
import requests
from firebase_admin import credentials, messaging
from flask import Flask, jsonify, make_response, render_template, request, send_file, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from itsdangerous import URLSafeSerializer
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / ".app-config"
(CONFIG_DIR / "Ultralytics").mkdir(parents=True, exist_ok=True)
(CONFIG_DIR / "matplotlib").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("YOLO_CONFIG_DIR", str(CONFIG_DIR))
os.environ.setdefault("MPLCONFIGDIR", str(CONFIG_DIR / "matplotlib"))

from ultralytics import YOLO

CONFIDENCE_THRESHOLD = 0.5
DB_PATH = BASE_DIR / "db" / "camerax.db"
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"
EVENT_IMAGE_DIR = BASE_DIR / "db" / "event_images"
FIREBASE_CREDENTIAL_PATH = BASE_DIR / "firebase-service-account.json"
FCM_TEST_TOKEN_PATH = CONFIG_DIR / "fcm-test-token.txt"
INFOBIP_CONFIG_PATH = CONFIG_DIR / "infobip.json"
TOKEN_SECRET = os.environ.get("TOKEN_SECRET", "camerax-demo-secret-key")
token_serializer = URLSafeSerializer(TOKEN_SECRET, salt="camerax-auth")

firebase_app = None
firebase_initialization_error = None
vital_alert_states = {}
vital_alert_lock = Lock()
try:
    firebase_app = firebase_admin.initialize_app(
        credentials.Certificate(FIREBASE_CREDENTIAL_PATH)
    )
except (FileNotFoundError, ValueError) as error:
    firebase_initialization_error = str(error)


def get_notification_token():
    token = os.environ.get("FCM_TEST_TOKEN")
    if token:
        return token.strip()
    if FCM_TEST_TOKEN_PATH.is_file():
        return FCM_TEST_TOKEN_PATH.read_text(encoding="utf-8").strip()
    return None


def send_mobile_notification(body, notification_data):
    if firebase_app is None:
        raise RuntimeError(firebase_initialization_error or "Firebase chưa được cấu hình.")
    fcm_token = get_notification_token()
    if not fcm_token:
        raise RuntimeError("Chưa cấu hình FCM token nhận thông báo.")

    message = messaging.Message(
        notification=messaging.Notification(
            title="Cảnh báo",
            body=body,
        ),
        data=notification_data,
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id="default",
                sound="notification_sound.wav",
            ),
        ),
        token=fcm_token,
    )
    return messaging.send(message, app=firebase_app)


def send_fall_notification(event_id=None):
    notification_data = {"type": "fall-event"}
    if event_id is not None:
        notification_data["event_id"] = str(event_id)
    return send_mobile_notification("Phát hiện ngã", notification_data)


def send_fall_notification_safely(event_id):
    try:
        send_fall_notification(event_id)
    except Exception as error:
        app.logger.error("Không thể gửi thông báo FCM cho event %s: %s", event_id, error)


def send_vital_notification_safely(camera_id, abnormal_vitals):
    labels = {
        "heart_rate": "nhịp tim",
        "spo2": "nồng độ oxy",
        "blood_pressure": "huyết áp",
    }
    vital_names = ", ".join(labels[name] for name in abnormal_vitals)
    try:
        send_mobile_notification(
            f"Phát hiện {vital_names} bất thường",
            {
                "type": "vital-alert",
                "camera_id": str(camera_id),
                "vitals": ",".join(abnormal_vitals),
            },
        )
    except Exception as error:
        app.logger.error(
            "Không thể gửi cảnh báo sinh hiệu camera %s: %s",
            camera_id,
            error,
        )


def get_infobip_config():
    file_config = {}
    if INFOBIP_CONFIG_PATH.is_file():
        file_config = json.loads(INFOBIP_CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "base_url": os.environ.get("INFOBIP_BASE_URL") or file_config.get("base_url"),
        "api_key": os.environ.get("INFOBIP_API_KEY") or file_config.get("api_key"),
        "from_number": os.environ.get("INFOBIP_FROM_NUMBER") or file_config.get("from_number"),
        "to_number": os.environ.get("INFOBIP_TEST_TO_NUMBER") or file_config.get("to_number"),
    }


def make_infobip_test_call():
    config = get_infobip_config()
    if not config["base_url"] or not config["api_key"]:
        raise RuntimeError("Chưa cấu hình Base URL hoặc API Key của Infobip.")
    if not config["from_number"] or not config["to_number"]:
        raise RuntimeError("Chưa cấu hình số gọi đi hoặc số nhận của Infobip.")

    response = requests.post(
        f"https://{config['base_url'].strip().rstrip('/')}/tts/3/advanced",
        json={
            "messages": [{
                "destinations": [{"to": config["to_number"]}],
                "from": config["from_number"],
                "language": "vi",
                "text": "Phát hiện ngã",
                "voice": {"gender": "female"},
            }],
        },
        headers={
            "Authorization": f"App {config['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=20,
    )
    if not response.ok:
        error_data = response.json()
        detail = (
            error_data.get("requestError", {})
            .get("serviceException", {})
            .get("text", response.text)
        )
        raise RuntimeError(detail)
    result = response.json()
    message = (result.get("messages") or [{}])[0]
    status = message.get("status") or {}
    return message.get("messageId"), status.get("name"), config["from_number"], config["to_number"]


def create_token(user):
    return token_serializer.dumps({
        "id": user["id"],
        "name": user["name"],
        "phone": user["phone"],
    })


@contextmanager
def get_db():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        existing_event_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(events)")
        }
        if "client_event_id" not in existing_event_columns:
            connection.execute("ALTER TABLE events ADD COLUMN client_event_id TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_client_event_id "
                "ON events(client_event_id)"
            )
        if "created_at" not in existing_event_columns:
            connection.execute("ALTER TABLE events ADD COLUMN created_at TEXT")
            connection.execute(
                "UPDATE events SET created_at = CURRENT_TIMESTAMP "
                "WHERE created_at IS NULL"
            )

        existing_notification_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(notifications)")
        }
        if "created_at" not in existing_notification_columns:
            connection.execute("ALTER TABLE notifications ADD COLUMN created_at TEXT")
            connection.execute(
                "UPDATE notifications SET created_at = CURRENT_TIMESTAMP "
                "WHERE created_at IS NULL"
            )

app = Flask(__name__, template_folder="templates")
# giới hạn kích thước tệp tải lên tối đa là 20MB
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
socket_rooms = {}
initialize_database()
model = YOLO(str(BASE_DIR / "ok.pt"))
# Khởi tạo trước PyTorch/YOLO để request phân tích đầu tiên không bị cold start.
model(np.zeros((640, 640, 3), dtype=np.uint8), verbose=False)


def signaling_room(camera_id):
    return f"camera:{camera_id}"


def get_signaling_data(data):
    data = data or {}
    camera_id = data.get("camera_id")
    if camera_id is None:
        emit("signaling-error", {"message": "camera_id is required."})
        return None, None
    return data, signaling_room(camera_id)


def forward_signaling_event(event_name, data, required_field):
    data, room = get_signaling_data(data)
    if data is None:
        return
    if required_field not in data:
        emit(
            "signaling-error",
            {"message": f"{required_field} is required for {event_name}."},
        )
        return

    target_sid = data.get("target_sid")
    emit(
        event_name,
        {
            "camera_id": data["camera_id"],
            required_field: data[required_field],
            "sender_sid": request.sid,
        },
        to=target_sid or room,
        include_self=False,
    )


@app.get("/")
def index():
    camera_id = request.args.get("camera_id", "1")
    response = make_response(render_template("index.html"))
    response.set_cookie("camera_id", camera_id, samesite="Lax")
    return response


@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    phone = data.get("phone")
    password = data.get("password")
    role = data.get("role")

    try:
        with get_db() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users (name, phone, password_hash, role)
                VALUES (?, ?, ?, ?)
                """,
                (name, phone, generate_password_hash(password, method="scrypt"), role),
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError as error:
        if "users.phone" in str(error):
            return jsonify(message="Số điện thoại đã được đăng ký."), 409
        return jsonify(message="Dữ liệu đăng ký không hợp lệ."), 400

    return jsonify(
        message="Đăng ký thành công.",
        user={"id": user_id, "name": name, "phone": phone, "role": role},
    ), 201


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    phone = data.get("phone")
    password = data.get("password")

    with get_db() as connection:
        user = connection.execute(
            """
            SELECT id, name, phone, password_hash, role
            FROM users
            WHERE phone = ?
            """,
            (phone,),
        ).fetchone()

    if user is None or not check_password_hash(user["password_hash"], password):
        return jsonify(message="Số điện thoại hoặc mật khẩu không đúng."), 401

    token = create_token(user)
    return jsonify(
        message="Đăng nhập thành công.",
        token=token,
        user={
            "id": user["id"],
            "name": user["name"],
            "phone": user["phone"],
            "role": user["role"],
        },
    )


@app.post("/api/calls/test")
def make_test_call():
    try:
        message_id, status, from_number, to_number = make_infobip_test_call()
    except (RuntimeError, ValueError, json.JSONDecodeError, requests.RequestException) as error:
        return jsonify(error="Không thể tạo cuộc gọi Infobip.", detail=str(error)), 502

    return jsonify(
        message="Đã yêu cầu Infobip thực hiện cuộc gọi.",
        message_id=message_id,
        status=status,
        from_number=from_number,
        to_number=to_number,
    )


@app.post("/api/fall-events")
def create_fall_event():
    data = request.get_json(silent=True) or {}
    camera_id = data.get("camera_id")
    client_event_id = data.get("client_event_id") or str(uuid4())
    images = (data.get("images") or [])[:10]

    with get_db() as connection:
        existing_event = connection.execute(
            "SELECT id FROM events WHERE client_event_id = ?",
            (client_event_id,),
        ).fetchone()
        if existing_event is not None:
            return jsonify(
                message="Sự kiện đã được lưu trước đó.",
                event_id=existing_event["id"],
                duplicate=True,
            )

        camera = connection.execute(
            "SELECT id, name FROM cameras WHERE id = ?",
            (camera_id,),
        ).fetchone()
        if camera is None:
            return jsonify(error="Không tìm thấy camera."), 404

    decoded_images = []
    try:
        for position, image in enumerate(images, start=1):
            image_data = image.get("image") or ""
            if not image_data.startswith("data:image/jpeg;base64,"):
                raise ValueError("Ảnh sự kiện phải là JPEG dạng data URL.")
            decoded_images.append((
                position,
                base64.b64decode(image_data.split(",", 1)[1], validate=True),
                image.get("captured_at"),
                float(image.get("confidence", 0)),
            ))
    except (TypeError, ValueError, base64.binascii.Error):
        return jsonify(error="Dữ liệu ảnh sự kiện không hợp lệ."), 400

    event_directory = None
    created_files = []
    try:
        with get_db() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events (
                    client_event_id, camera_id, type, started_at, last_seen_at,
                    ended_at, signal_count, max_confidence, created_at
                ) VALUES (?, ?, 'fall', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    client_event_id,
                    camera_id,
                    data.get("started_at"),
                    data.get("ended_at"),
                    data.get("ended_at"),
                    int(data.get("signal_count", 1)),
                    float(data.get("max_confidence", 0)),
                ),
            )
            event_id = cursor.lastrowid
            event_directory = EVENT_IMAGE_DIR / str(event_id)
            event_directory.mkdir(parents=True, exist_ok=True)

            for position, image_bytes, captured_at, confidence in decoded_images:
                image_path = event_directory / f"{position}.jpg"
                image_path.write_bytes(image_bytes)
                created_files.append(image_path)
                relative_path = image_path.relative_to(BASE_DIR).as_posix()
                connection.execute(
                    """
                    INSERT INTO event_images (
                        event_id, image_path, captured_at, confidence, position
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (event_id, relative_path, captured_at, confidence, position),
                )

            notification_cursor = connection.execute(
                """
                INSERT INTO notifications (event_id, supervisor_id, created_at)
                SELECT ?, supervisor_id, CURRENT_TIMESTAMP
                FROM supervisor_cameras
                WHERE camera_id = ?
                """,
                (event_id, camera_id),
            )
            notification_count = notification_cursor.rowcount
            created_at = connection.execute(
                "SELECT created_at FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()["created_at"]
    except (OSError, sqlite3.Error, TypeError, ValueError):
        for image_path in created_files:
            image_path.unlink(missing_ok=True)
        if event_directory is not None:
            try:
                event_directory.rmdir()
            except OSError:
                pass
        return jsonify(error="Không thể lưu sự kiện ngã."), 400

    event_payload = {
        "id": event_id,
        "client_event_id": client_event_id,
        "camera_id": camera_id,
        "camera_name": camera["name"],
        "type": "fall",
        "started_at": data.get("started_at"),
        "last_seen_at": data.get("ended_at"),
        "ended_at": data.get("ended_at"),
        "signal_count": int(data.get("signal_count", 1)),
        "max_confidence": float(data.get("max_confidence", 0)),
        "created_at": created_at,
        "image_count": len(decoded_images),
        "images": [
            {
                "position": position,
                "captured_at": captured_at,
                "confidence": confidence,
                "image_url": url_for(
                    "get_fall_event_image",
                    event_id=event_id,
                    position=position,
                ),
            }
            for position, _image_bytes, captured_at, confidence in decoded_images
        ],
    }
    socketio.emit(
        "fall-event",
        event_payload,
        to=signaling_room(camera_id),
    )
    socketio.start_background_task(send_fall_notification_safely, event_id)
    return jsonify(
        message="Đã lưu sự kiện ngã.",
        event=event_payload,
        notification_count=notification_count,
    ), 201


@app.get("/api/fall-events")
def get_fall_events():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    camera_id = request.args.get("camera_id")
    supervisor_id = request.args.get("supervisor_id")
    conditions = ["e.type = 'fall'"]
    parameters = []

    if camera_id is not None:
        conditions.append("e.camera_id = ?")
        parameters.append(camera_id)
    if supervisor_id is not None:
        conditions.append(
            "EXISTS ("
            "SELECT 1 FROM notifications n "
            "WHERE n.event_id = e.id AND n.supervisor_id = ?"
            ")"
        )
        parameters.append(supervisor_id)

    where_clause = " AND ".join(conditions)
    page_size = 10
    offset = (page - 1) * page_size

    with get_db() as connection:
        total_items = connection.execute(
            f"SELECT COUNT(*) FROM events e WHERE {where_clause}",
            parameters,
        ).fetchone()[0]
        event_rows = connection.execute(
            f"""
            SELECT
                e.id, e.client_event_id, e.camera_id, c.name AS camera_name,
                e.type, e.started_at, e.last_seen_at, e.ended_at,
                e.signal_count, e.max_confidence, e.created_at
            FROM events e
            JOIN cameras c ON c.id = e.camera_id
            WHERE {where_clause}
            ORDER BY e.id DESC
            LIMIT ? OFFSET ?
            """,
            [*parameters, page_size, offset],
        ).fetchall()

        event_ids = [row["id"] for row in event_rows]
        images_by_event = {event_id: [] for event_id in event_ids}
        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            image_rows = connection.execute(
                f"""
                SELECT event_id, captured_at, confidence, position
                FROM event_images
                WHERE event_id IN ({placeholders})
                ORDER BY event_id DESC, position ASC
                """,
                event_ids,
            ).fetchall()
            for image in image_rows:
                images_by_event[image["event_id"]].append({
                    "position": image["position"],
                    "captured_at": image["captured_at"],
                    "confidence": image["confidence"],
                    "image_url": url_for(
                        "get_fall_event_image",
                        event_id=image["event_id"],
                        position=image["position"],
                    ),
                })

    events = []
    for row in event_rows:
        events.append({
            "id": row["id"],
            "client_event_id": row["client_event_id"],
            "camera_id": row["camera_id"],
            "camera_name": row["camera_name"],
            "type": row["type"],
            "started_at": row["started_at"],
            "last_seen_at": row["last_seen_at"],
            "ended_at": row["ended_at"],
            "signal_count": row["signal_count"],
            "max_confidence": row["max_confidence"],
            "created_at": row["created_at"],
            "images": images_by_event[row["id"]],
        })

    total_pages = (total_items + page_size - 1) // page_size
    return jsonify(
        page=page,
        page_size=page_size,
        total_items=total_items,
        total_pages=total_pages,
        has_next=page < total_pages,
        events=events,
    )


@app.get("/api/fall-events/<int:event_id>/images/<int:position>")
def get_fall_event_image(event_id, position):
    with get_db() as connection:
        image = connection.execute(
            """
            SELECT image_path
            FROM event_images
            WHERE event_id = ? AND position = ?
            """,
            (event_id, position),
        ).fetchone()

    if image is None:
        return jsonify(error="Không tìm thấy ảnh sự kiện."), 404

    image_path = (BASE_DIR / image["image_path"]).resolve()
    if not image_path.is_relative_to(BASE_DIR.resolve()) or not image_path.is_file():
        return jsonify(error="File ảnh sự kiện không tồn tại."), 404
    return send_file(image_path, mimetype="image/jpeg")


@socketio.on("join-room")
def handle_join_room(data):
    data, room = get_signaling_data(data)
    if data is None:
        return

    peer_role = data.get("peer_role")
    if peer_role not in {"sender", "viewer"}:
        emit("signaling-error", {"message": "peer_role must be sender or viewer."})
        return

    camera_id = data["camera_id"]
    join_room(room)
    socket_rooms.setdefault(request.sid, set()).add((room, camera_id, peer_role))
    emit(
        "room-joined",
        {
            "camera_id": camera_id,
            "peer_role": peer_role,
            "socket_id": request.sid,
        },
    )
    emit(
        "peer-joined",
        {
            "camera_id": camera_id,
            "peer_role": peer_role,
            "socket_id": request.sid,
        },
        to=room,
        include_self=False,
    )


@socketio.on("viewer-ready")
def handle_viewer_ready(data):
    data, room = get_signaling_data(data)
    if data is None:
        return
    emit(
        "viewer-ready",
        {"camera_id": data["camera_id"], "viewer_sid": request.sid},
        to=room,
        include_self=False,
    )


@socketio.on("webrtc-offer")
def handle_webrtc_offer(data):
    forward_signaling_event("webrtc-offer", data, "sdp")


@socketio.on("webrtc-answer")
def handle_webrtc_answer(data):
    forward_signaling_event("webrtc-answer", data, "sdp")


@socketio.on("ice-candidate")
def handle_ice_candidate(data):
    forward_signaling_event("ice-candidate", data, "candidate")


@socketio.on("watch-vitals")
def handle_watch_vitals(data):
    data, room = get_signaling_data(data)
    if data is None:
        return
    vitals = data.get("vitals")
    if not isinstance(vitals, dict):
        emit("signaling-error", {"message": "vitals must be an object."})
        return

    emit(
        "watch-vitals",
        {
            "camera_id": data["camera_id"],
            "measured_at": data.get("measured_at"),
            "vitals": vitals,
            "sender_sid": request.sid,
        },
        to=room,
        include_self=False,
    )

    camera_key = str(data["camera_id"])
    tracked_vitals = ("heart_rate", "spo2", "blood_pressure")
    newly_abnormal = []
    with vital_alert_lock:
        camera_states = vital_alert_states.setdefault(camera_key, {})
        for vital_name in tracked_vitals:
            vital_data = vitals.get(vital_name)
            if not isinstance(vital_data, dict):
                continue
            current_state = vital_data.get("state")
            was_normal = camera_states.get(vital_name, "normal") == "normal"
            is_abnormal = current_state not in {None, "normal"}
            if was_normal and is_abnormal:
                newly_abnormal.append(vital_name)
            camera_states[vital_name] = current_state

    if newly_abnormal:
        socketio.start_background_task(
            send_vital_notification_safely,
            data["camera_id"],
            newly_abnormal,
        )


@socketio.on("stream-ended")
def handle_stream_ended(data):
    data, room = get_signaling_data(data)
    if data is None:
        return
    emit(
        "stream-ended",
        {"camera_id": data["camera_id"], "sender_sid": request.sid},
        to=room,
        include_self=False,
    )


@socketio.on("leave-room")
def handle_leave_room(data):
    data, room = get_signaling_data(data)
    if data is None:
        return

    memberships = socket_rooms.get(request.sid, set())
    for membership in [item for item in memberships if item[0] == room]:
        memberships.discard(membership)
    if not memberships:
        socket_rooms.pop(request.sid, None)

    leave_room(room)
    emit(
        "peer-left",
        {"camera_id": data["camera_id"], "socket_id": request.sid},
        to=room,
    )


@socketio.on("disconnect")
def handle_disconnect():
    for room, camera_id, peer_role in socket_rooms.pop(request.sid, set()):
        emit(
            "peer-left",
            {
                "camera_id": camera_id,
                "peer_role": peer_role,
                "socket_id": request.sid,
            },
            to=room,
        )


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

    response_data = {
        "camera_id": request.form.get("camera_id") or request.cookies.get("camera_id", "1"),
        "has_fall": has_fall,
        "detections": detections,
        "frame_width": frame.shape[1],
        "frame_height": frame.shape[0],
        "server_time": datetime.now().strftime("%H:%M:%S %d/%m/%Y"),
        "frame_image": frame_image,
        "processing_ms": round((time.perf_counter() - started_at) * 1000),
    }
    socketio.emit(
        "analysis-result",
        {
            "camera_id": response_data["camera_id"],
            "has_fall": response_data["has_fall"],
            "detections": response_data["detections"],
            "frame_width": response_data["frame_width"],
            "frame_height": response_data["frame_height"],
            "server_time": response_data["server_time"],
            "processing_ms": response_data["processing_ms"],
        },
        to=signaling_room(response_data["camera_id"]),
    )

    return jsonify(**response_data)



if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )
