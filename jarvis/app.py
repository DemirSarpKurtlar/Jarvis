"""Uygulama başlatma ve kapatma akışı."""

from __future__ import annotations

import argparse
import asyncio
import logging
import platform
import sys
import time

from jarvis import APP_NAME, __version__
from jarvis.config.settings import SettingsStore
from jarvis.core.errors import asyncio_exception_handler, install_exception_hooks
from jarvis.core.logging_setup import setup_logging
from jarvis.paths import AppPaths

log = logging.getLogger("jarvis")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="jarvis", description="JARVIS kişisel asistan")
    p.add_argument("--debug", action="store_true", help="Logları konsola da yaz")
    p.add_argument("--gelistirici", action="store_true",
                   help="Geliştirici kısayollarını aç (F8: durumları dene, F9: deneme hatası)")
    p.add_argument("--smoke-test", type=float, metavar="SANIYE",
                   help="Verilen süre sonra kendini kapat (otomatik testler için)")
    return p.parse_args(argv)


def _install_qt_message_handler() -> None:
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    qt_log = logging.getLogger("qt")
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode, _context, message):  # noqa: ANN001
        qt_log.log(levels.get(mode, logging.WARNING), message)

    qInstallMessageHandler(handler)


def configure_application(app):  # noqa: ANN001, ANN201
    """QApplication ayarları. Uygulama ve testler aynı yapılandırmayı kullanır."""
    from PySide6.QtGui import QFont

    from jarvis.interface import theme

    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    # Pencere kapanınca Qt döngüsünü hemen durdurma: önce asistan temizce kapanmalı
    app.setQuitOnLastWindowClosed(False)
    fonts = theme.load_fonts()
    app.setFont(QFont(fonts.body, 10))
    app.setStyleSheet(theme.stylesheet(fonts))
    return fonts


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    t0 = time.perf_counter()

    paths = AppPaths.resolve()
    setup_logging(paths.log_file, "INFO", console=args.debug)
    install_exception_hooks()
    log.info("%s %s başlatılıyor (Python %s, %s)", APP_NAME, __version__,
             platform.python_version(), platform.platform())

    store = SettingsStore(paths.settings_file)
    settings = store.load()
    if settings.general.log_level != "INFO":
        setup_logging(paths.log_file, settings.general.log_level, console=args.debug)

    # Qt modülleri burada içe aktarılır ki loglama her şeyden önce hazır olsun
    import qasync
    from PySide6.QtWidgets import QApplication

    from jarvis.core.assistant import Assistant
    from jarvis.interface.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    fonts = configure_application(app)
    _install_qt_message_handler()

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(asyncio_exception_handler)

    assistant = Assistant(store)
    window = MainWindow(assistant, fonts, developer=args.gelistirici)

    async def run() -> None:
        closed = asyncio.Event()
        window.close_requested.connect(closed.set)
        # İşletim sistemi oturumu kapatırken döngü beklenmeden durabilir: ayarları hemen kaydet
        app.aboutToQuit.connect(store.save)
        await assistant.start()
        if settings.ui.start_minimized:
            window.showMinimized()
        else:
            window.show()
        log.info("Arayüz hazır (%.0f ms)", (time.perf_counter() - t0) * 1000)
        if args.smoke_test:
            loop.call_later(args.smoke_test, window.close)
        await closed.wait()
        await assistant.shutdown()

    try:
        with loop:
            loop.run_until_complete(run())
    except Exception:
        log.critical("Uygulama beklenmedik şekilde sonlandı", exc_info=True)
        return 1
    log.info("%s kapatıldı", APP_NAME)
    logging.shutdown()
    return 0
