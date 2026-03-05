"""
main.py  –  NEUROSENSE entry point.

Usage
-----
    python main.py              # normal run
    python main.py --debug      # verbose logging + Flask debug mode

Open the dashboard at:  http://<pi-ip>:5000
"""

import argparse
import logging
import signal
import sys
import time

import config
from logging_module.csv_logger import CSVLogger
from sensors.sensor_manager import SensorManager
from dashboard.app import create_app
from experiments.session_manager import SessionManager
from experiments.respondent_registry import RespondentRegistry


# ── Logging setup ────────────────────────────────────────────────────────────

def setup_logging(debug: bool = False):
    level = logging.DEBUG if debug else logging.INFO
    fmt   = "%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("neurosense.log", encoding="utf-8"),
        ],
    )
    # Suppress noisy Flask/Werkzeug logs unless in debug mode
    if not debug:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NEUROSENSE Sensor System")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging and Flask debug mode")
    args = parser.parse_args()

    setup_logging(debug=args.debug)
    logger = logging.getLogger("main")
    logger.info("═" * 60)
    logger.info("NEUROSENSE starting (schema v%s)", config.DATA_SCHEMA_VERSION)
    logger.info("═" * 60)

    # ── Start CSV logger ─────────────────────────────────────────────────
    csv_logger = CSVLogger()
    csv_logger.start()

    # ── Start sensor manager ─────────────────────────────────────────────
    sensor_manager = SensorManager(csv_logger=csv_logger)
    sensor_manager.start()

    # ── Start camera ───────────────────────────────────────────────────
    camera_reader = None
    if config.CAMERA_ENABLED:
        try:
            from sensors.camera_reader import CameraReader
            camera_reader = CameraReader()
            camera_reader.start()
            logger.info("CameraReader started")
        except Exception as exc:
            logger.error("Camera failed to start — continuing without camera: %s", exc)
            camera_reader = None

    # ── Give sensors a moment to get first readings ────────────────────
    time.sleep(2)

    # ── Initialise experiment modules ────────────────────────────────
    respondent_registry = RespondentRegistry()
    session_manager     = SessionManager(
        sensor_manager=sensor_manager,
        camera_reader=camera_reader,
    )
    logger.info("SessionManager and RespondentRegistry initialised")

    # ── Create Flask app ────────────────────────────────────────
    app = create_app(
        sensor_manager,
        camera_reader=camera_reader,
        session_manager=session_manager,
        respondent_registry=respondent_registry,
    )

    # ── Graceful shutdown on Ctrl+C / SIGTERM ────────────────────────────
    def shutdown(signum, frame):
        logger.info("Shutdown signal received (%s).", signal.Signals(signum).name)
        # Stop active session first so data is finalized cleanly before threads die
        try:
            if session_manager.get_active_session() is not None:
                logger.info("Active session detected — stopping before shutdown...")
                session_manager.stop_session()
        except Exception as exc:
            logger.warning("Could not stop active session during shutdown: %s", exc)
        sensor_manager.stop()
        csv_logger.stop()
        if camera_reader is not None:
            camera_reader.stop()
        logger.info("NEUROSENSE stopped cleanly.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Run Flask ─────────────────────────────────────────────────────────
    logger.info(
        "Dashboard running at http://%s:%d",
        config.DASHBOARD_HOST, config.DASHBOARD_PORT,
    )
    app.run(
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        threaded=True,
        debug=args.debug,
        use_reloader=False,   # reloader conflicts with sensor threads
    )


if __name__ == "__main__":
    main()
