"""
dashboard/app.py  –  Flask web dashboard with Server-Sent Events (SSE).

Routes
------
GET  /                              → dashboard HTML page
GET  /stream                        → SSE stream
GET  /health                        → JSON health check
GET  /camera/stream                 → MJPEG live video
GET  /camera/snapshot               → high-res JPEG

Experiment routes
-----------------
GET  /experiment                    → session control panel
GET  /experiment/respondents        → respondent management
POST /experiment/respondents/add    → register new respondent
DEL  /experiment/respondents/<id>   → delete respondent
GET  /experiment/sessions           → all sessions list
GET  /experiment/session/<id>       → session detail JSON
GET  /experiment/session/active     → active session JSON
POST /experiment/session/start      → start recording session
POST /experiment/session/stop       → stop recording session
"""

import json
import logging
import time
from typing import Optional
from flask import (
    Flask, Response, jsonify, redirect, render_template,
    request, stream_with_context, url_for,
)

import config

logger = logging.getLogger(__name__)

# Injected by main.py after startup
_sensor_manager    = None
_camera_reader:    Optional[object] = None
_session_manager:  Optional[object] = None
_respondent_registry: Optional[object] = None


def create_app(
    sensor_manager,
    camera_reader=None,
    session_manager=None,
    respondent_registry=None,
) -> Flask:
    """
    Factory function — creates and configures the Flask app.

    Parameters
    ----------
    sensor_manager      : SensorManager         (required)
    camera_reader       : CameraReader          (optional)
    session_manager     : SessionManager        (optional)
    respondent_registry : RespondentRegistry    (optional)
    """
    global _sensor_manager, _camera_reader, _session_manager, _respondent_registry
    _sensor_manager      = sensor_manager
    _camera_reader       = camera_reader
    _session_manager     = session_manager
    _respondent_registry = respondent_registry

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
            while True:
                # Blocks until capture thread signals a new frame (zero delay).
                # Timeout 0.5 s keeps connection alive if camera stalls briefly.
                frame = _camera_reader.get_new_frame(timeout=0.5)
                if frame is not None:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + frame
                        + b"\r\n"
                    )
                    # get_new_frame() already waited for THIS frame to be new —
                    # immediately loop back so we never sleep between frames.

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control":     "no-cache",
                "X-Accel-Buffering": "no",
                # Tell browser to display frames immediately, don't buffer
                "Transfer-Encoding": "chunked",
            },
        )

    @app.route("/camera/snapshot")
    def camera_snapshot():
        """Return a high-res JPEG from the main stream (1920x1080)."""
        if _camera_reader is None:
            return Response("Camera not enabled", status=503)
        frame = _camera_reader.capture_snapshot()
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

    # ──────────────────────────────────────────────────────────────────
    # Experiment routes
    # ──────────────────────────────────────────────────────────────────

    def _require_sm():
        """Return (session_manager, respondent_registry) or raise 503."""
        if _session_manager is None or _respondent_registry is None:
            return None, None
        return _session_manager, _respondent_registry

    @app.route("/experiment")
    def experiment():
        """Main experiment control panel."""
        sm, rr = _require_sm()
        if sm is None:
            return Response("Experiment module not initialised", status=503)
        respondents = rr.get_all()
        active      = sm.get_active_session()
        duration    = getattr(config, "EXPERIMENT_SESSION_DURATION_S", 60)
        return render_template(
            "experiment.html",
            respondents=respondents,
            active_session=active,
            default_duration=duration,
        )

    @app.route("/experiment/respondents")
    def experiment_respondents():
        """Respondent management page."""
        sm, rr = _require_sm()
        if rr is None:
            return Response("Experiment module not initialised", status=503)
        return render_template(
            "respondents.html",
            respondents=rr.get_all(),
            next_id=rr.next_id(),
        )

    @app.route("/experiment/respondents/add", methods=["POST"])
    def experiment_respondents_add():
        """Register a new respondent (form POST)."""
        sm, rr = _require_sm()
        if rr is None:
            return jsonify({"status": "error", "message": "Module not ready"}), 503
        rid    = request.form.get("respondent_id", "").strip().upper()
        gender = request.form.get("gender", "M").strip().upper()
        age    = request.form.get("age", "0").strip()
        notes  = request.form.get("notes", "").strip()
        try:
            if not rid:
                rid = rr.next_id()
            rr.add(rid, gender, int(age), notes)
            return redirect(url_for("experiment_respondents"))
        except ValueError as exc:
            return render_template(
                "respondents.html",
                respondents=rr.get_all(),
                next_id=rr.next_id(),
                error=str(exc),
            ), 400

    @app.route("/experiment/respondents/<respondent_id>", methods=["DELETE"])
    def experiment_respondents_delete(respondent_id: str):
        sm, rr = _require_sm()
        if rr is None:
            return jsonify({"status": "error"}), 503
        deleted = rr.delete(respondent_id)
        return jsonify({"status": "ok" if deleted else "not_found"})

    @app.route("/experiment/sessions")
    def experiment_sessions():
        """All sessions list."""
        sm, rr = _require_sm()
        if sm is None:
            return Response("Experiment module not initialised", status=503)
        sessions = sm.list_sessions()
        return render_template("sessions.html", sessions=sessions)

    @app.route("/experiment/session/active")
    def experiment_session_active():
        """JSON: active session info (or null)."""
        sm, _ = _require_sm()
        if sm is None:
            return jsonify(None)
        return jsonify(sm.get_active_session())

    @app.route("/experiment/session/<session_id>")
    def experiment_session_detail(session_id: str):
        """JSON: metadata for a specific session."""
        sm, _ = _require_sm()
        if sm is None:
            return jsonify({"status": "error"}), 503
        meta = sm.get_session(session_id)
        if meta is None:
            return jsonify({"status": "not_found"}), 404
        return jsonify(meta)

    @app.route("/experiment/session/start", methods=["POST"])
    def experiment_session_start():
        """Start a new recording session (JSON or form POST)."""
        sm, _ = _require_sm()
        if sm is None:
            return jsonify({"status": "error", "message": "Module not ready"}), 503
        data         = request.get_json(silent=True) or request.form
        respondent   = data.get("respondent_id", "")
        duration_raw = data.get("duration_sec", None)
        duration     = int(duration_raw) if duration_raw else None
        if not respondent:
            return jsonify({"status": "error",
                            "message": "respondent_id required"}), 400
        try:
            meta = sm.start_session(respondent, duration_sec=duration)
            # If form POST (browser), redirect back to experiment page
            if request.form:
                return redirect(url_for("experiment"))
            return jsonify({"status": "ok", "session": meta})
        except (RuntimeError, ValueError) as exc:
            if request.form:
                return redirect(url_for("experiment"))
            return jsonify({"status": "error", "message": str(exc)}), 400

    @app.route("/experiment/session/stop", methods=["POST"])
    def experiment_session_stop():
        """Stop the active recording session."""
        sm, _ = _require_sm()
        if sm is None:
            return jsonify({"status": "error", "message": "Module not ready"}), 503
        meta = sm.stop_session()
        if meta is None:
            if request.form:
                return redirect(url_for("experiment"))
            return jsonify({"status": "error", "message": "No active session"}), 400
        if request.form:
            return redirect(url_for("experiment_sessions"))
        return jsonify({"status": "ok", "session": meta})

    return app
