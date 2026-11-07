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
    # Aşama 9
    p.add_argument("--baslangic", action="store_true",
                   help="Windows açılışında başlatıldı: pencereyi gösterme, bildirim alanında bekle")
    p.add_argument("--kapat", action="store_true",
                   help="Çalışan JARVIS'i temizce kapat ve kapanmasını bekle (kurulum programı kullanır)")
    p.add_argument("--anahtari-sil", action="store_true",
                   help="Kayıtlı Claude API anahtarını sil ve çık (kaldırma programı kullanır)")
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
    from jarvis.interface.app_icon import app_icon  # Aşama 9

    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())  # Aşama 9
    # Pencere kapanınca Qt döngüsünü hemen durdurma: önce asistan temizce kapanmalı
    app.setQuitOnLastWindowClosed(False)
    fonts = theme.load_fonts()
    app.setFont(QFont(fonts.body, 10))
    app.setStyleSheet(theme.stylesheet(fonts))
    return fonts


# --- Aşama 9: tek örnek, Windows ile başlatma, bildirim alanı (yardımcılar) ---
WINDOWS_APP_ID = "JARVIS.Asistan"  # kurulumdaki kısayolla aynı (görev çubuğu gruplaması)


def _set_windows_app_id() -> None:
    """Kaynaktan çalışırken görev çubuğunda Python simgesi yerine JARVIS simgesi görünsün."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except Exception:  # noqa: BLE001
        log.debug("AppUserModelID ayarlanamadı", exc_info=True)


def _close_running_instance(instance) -> int:  # noqa: ANN001  (SingleInstance)
    """`--kapat`: çalışan örneğe kapan mesajı gönderir ve kilidi bırakmasını bekler."""
    from jarvis.integrations.single_instance import MSG_QUIT

    if instance.acquire():
        instance.release()
        log.info("--kapat: çalışan JARVIS yok")
        return 0
    log.info("--kapat: çalışan JARVIS'e kapanma isteği gönderiliyor")
    if instance.send(MSG_QUIT) and instance.wait_until_released(timeout_s=30):
        log.info("--kapat: çalışan JARVIS kapandı")
        return 0
    log.warning("--kapat: çalışan JARVIS zamanında kapanmadı")
    return 1


def _log_credential_backend() -> None:
    try:
        from jarvis.security.credentials import CredentialStore

        cs = CredentialStore()
        log.info("Anahtar deposu: %s (%s)", cs.backend_name(), "güvenli" if cs.is_secure() else "güvenli değil")
    except Exception:  # noqa: BLE001
        log.warning("Anahtar deposu belirlenemedi", exc_info=True)
# --- Aşama 9 (son) ---


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

    # --- Aşama 9: tek örnek ve Windows ile başlatma ---
    from jarvis.integrations.autostart import Autostart
    from jarvis.integrations.single_instance import MSG_ACTIVATE, MSG_QUIT, SingleInstance

    if args.anahtari_sil:
        from jarvis.security.credentials import CredentialStore

        CredentialStore().delete_anthropic_key()
        logging.shutdown()
        return 0
    instance = SingleInstance(paths.data_dir)
    if args.kapat:
        code = _close_running_instance(instance)
        logging.shutdown()
        return code
    if not instance.acquire():
        if args.baslangic:
            log.info("JARVIS zaten çalışıyor; Windows açılışındaki ikinci başlatma yok sayıldı")
        else:
            log.info("JARVIS zaten çalışıyor; çalışan pencere öne getiriliyor")
            instance.send(MSG_ACTIVATE)
        logging.shutdown()
        return 0
    _set_windows_app_id()
    Autostart().sync(store)
    _log_credential_backend()
    # --- Aşama 9 (son) ---

    fonts = configure_application(app)
    _install_qt_message_handler()

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(asyncio_exception_handler)

    assistant = Assistant(store)
    window = MainWindow(assistant, fonts, developer=args.gelistirici)

    # --- Aşama 9: bildirim alanı ve ikinci açılıştan gelen mesajlar ---
    from jarvis.interface.tray import TrayController

    tray = TrayController.create(window, assistant)
    instance.message_received.connect(
        lambda msg: window.quit_app() if msg == MSG_QUIT else window.bring_to_front())
    # --- Aşama 9 (son) ---

    async def run() -> None:
        closed = asyncio.Event()
        window.close_requested.connect(closed.set)
        # İşletim sistemi oturumu kapatırken döngü beklenmeden durabilir: ayarları hemen kaydet
        app.aboutToQuit.connect(store.save)
        await assistant.start()
        if args.baslangic and tray is not None:
            log.info("Windows açılışında başlatıldı: pencere bildirim alanında bekliyor")
        elif settings.ui.start_minimized or args.baslangic:
            window.showMinimized()
        else:
            window.show()
        log.info("Arayüz hazır (%.0f ms)", (time.perf_counter() - t0) * 1000)
        if args.smoke_test:
            loop.call_later(args.smoke_test, window.quit_app)
        await closed.wait()
        await assistant.shutdown()

    try:
        with loop:
            loop.run_until_complete(run())
    except Exception:
        log.critical("Uygulama beklenmedik şekilde sonlandı", exc_info=True)
        return 1
    finally:
        instance.release()  # Aşama 9
    log.info("%s kapatıldı", APP_NAME)
    logging.shutdown()
    return 0
