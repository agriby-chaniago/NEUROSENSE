"""
dashboard/app.py  –  Flask web dashboard with Server-Sent Events (SSE).

Routes
------
GET /           → dashboard HTML page
GET /stream     → SSE stream (data pushed every DASHBOARD_SSE_INTERVAL_S)
GET /health     → JSON health check for all sensors
GET /snapshot   → JSON single latest reading (useful for debugging)
"""

import json
import logging
import time
from typing import Optional
from flask import Flask, Response, jsonify, render_template

import config

logger = logging.getLogger(__name__)

# Injected by main.py after startup
_sensor_manager = None
_camera_reader: Optional[object] = None


def create_app(sensor_manager, camera_reader=None) -> Flask:
    """
    Factory function — creates and configures the Flask app.

    Parameters
    ----------
    sensor_manager : SensorManager
        Already-started SensorManager instance.
    """
    global _sensor_manager, _camera_reader
    _sensor_manager = sensor_manager
    _camera_reader  = camera_reader

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SECRET_KEY"] = "neurosense-dev-key"

    # ── Routes ────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/stream")
    def stream():
        """
        SSE endpoint.  The browser opens one persistent connection here and
        receives JSON data frames every DASHBOARD_SSE_INTERVAL_S seconds.
        """
        def event_generator():
            while True:
                data = _sensor_manager.get_latest()
                # Convert None to JSON null cleanly
                payload = json.dumps(data, default=lambda x: None)
                yield f"data: {payload}\n\n"
                time.sleep(config.DASHBOARD_SSE_INTERVAL_S)

        return Response(
            stream_with_context(event_generator()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control":  "no-cache",
                "X-Accel-Buffering": "no",   # disable nginx buffering if proxied
            },
        )

    @app.route("/health")
    def health():
        """JSON health check — shows per-sensor status."""
        sensors = _sensor_manager.health()
        if _camera_reader is not None:
            sensors.append(_camera_reader.health())
        return jsonify({"status": "ok", "sensors": sensors})

    @app.route("/camera/stream")
    def camera_stream():
        """
        MJPEG multipart stream — point an <img> src here for live video.
        Falls back to a 503 if the camera is not enabled/available.
        """
        if _camera_reader is None:
            return Response("Camera not enabled", status=503)

        def generate():
            interval = 1.0 / max(1, config.CAMERA_FRAMERATE)
            while True:
                frame = _camera_reader.get_frame()
                if frame is not None:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + frame
                        + b"\r\n"
                    )
                time.sleep(interval)

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/camera/snapshot")
    def camera_snapshot():
        """Return the latest JPEG frame as a single image (useful for diagnostics)."""
        if _camera_reader is None:
            return Response("Camera not enabled", status=503)
        frame = _camera_reader.get_frame()
        if frame is None:
            return Response("No frame yet", status=503)
        return Response(frame, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-cache"})

    @app.route("/snapshot")
    def snapshot():
        """Return a single JSON snapshot of latest sensor readings."""
        return jsonify(_sensor_manager.get_latest())

    @app.route("/recalibrate/<sensor_name>", methods=["POST"])
    def recalibrate(sensor_name: str):
        """
        Trigger runtime recalibration for a sensor.
        POST /recalibrate/gsr  → retakes GSR baseline (sensor must NOT be worn)
        """
        try:
            result = _sensor_manager.recalibrate_sensor(sensor_name)
            logger.info("Recalibrated '%s': %s", sensor_name, result)
            return jsonify({"status": "ok", "sensor": sensor_name, **result})
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 404
        except NotImplementedError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400
        except Exception as exc:
            logger.error("Recalibrate '%s' failed: %s", sensor_name, exc)
            return jsonify({"status": "error", "message": str(exc)}), 500

    return app
