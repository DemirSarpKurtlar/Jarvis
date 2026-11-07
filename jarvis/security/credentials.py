"""Gizli bilgilerin güvenli saklanması.

API anahtarı Windows'ta **Windows Credential Manager**'a (keyring kütüphanesi)
yazılır; asla ayar dosyasına, loga ya da veritabanına yazılmaz.

Güvenli bir anahtar deposu bulunamazsa (ör. düz metin dosyasına yazan bir
arka uç) anahtar kaydedilmez ve kullanıcıya nedeni söylenir.

Geliştirme ve otomatik testler için `JARVIS_ANTHROPIC_API_KEY` ortam değişkeni
okunabilir (kasıtlı olarak genel `ANTHROPIC_API_KEY` değil: başka bir aracın
anahtarı yanlışlıkla kullanılmasın).
"""

from __future__ import annotations

import logging
import os
import re

import keyring
import keyring.errors

from jarvis.core.errors import JarvisError

log = logging.getLogger(__name__)

SERVICE = "JARVIS"
ANTHROPIC_ACCOUNT = "anthropic_api_key"
ENV_OVERRIDE = "JARVIS_ANTHROPIC_API_KEY"

_KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,}$")
# Düz metin ya da hiç saklamayan arka uçlar güvenli sayılmaz
_INSECURE_BACKENDS = ("fail.Keyring", "null.Keyring", "PlaintextKeyring", "chainer.ChainerBackend")


class CredentialError(JarvisError):
    pass


def looks_like_anthropic_key(value: str) -> bool:
    return bool(_KEY_RE.match(value.strip()))


class CredentialStore:
    def backend_name(self) -> str:
        kr = keyring.get_keyring()
        name = f"{type(kr).__module__}.{type(kr).__name__}"
        if "windows" in name.lower():
            return "Windows Credential Manager"
        return name

    def is_secure(self) -> bool:
        kr = keyring.get_keyring()
        full = f"{type(kr).__module__}.{type(kr).__name__}"
        if any(full.endswith(bad) for bad in _INSECURE_BACKENDS):
            # ChainerBackend güvenli olabilir; içinde gerçek bir arka uç var mı bak
            if full.endswith("ChainerBackend"):
                return any(not f"{type(b).__module__}.{type(b).__name__}".endswith(_INSECURE_BACKENDS)
                           for b in getattr(kr, "backends", []))
            return False
        return getattr(kr, "priority", 0) > 0

    def get_anthropic_key(self) -> str | None:
        env = os.environ.get(ENV_OVERRIDE)
        if env:
            return env.strip()
        try:
            return keyring.get_password(SERVICE, ANTHROPIC_ACCOUNT)
        except keyring.errors.KeyringError as exc:
            log.warning("Anahtar deposu okunamadı: %s", exc.__class__.__name__)
            return None

    def has_anthropic_key(self) -> bool:
        return bool(self.get_anthropic_key())

    def set_anthropic_key(self, key: str) -> None:
        key = key.strip()
        if not looks_like_anthropic_key(key):
            raise CredentialError("Geçersiz anahtar biçimi",
                                  "Bu bir Claude API anahtarına benzemiyor. Anahtar 'sk-ant-' ile başlamalı.")
        if not self.is_secure():
            raise CredentialError(
                f"Güvenli anahtar deposu yok ({self.backend_name()})",
                "Bu bilgisayarda güvenli bir anahtar deposu bulunamadı, anahtar kaydedilmedi.",
            )
        try:
            keyring.set_password(SERVICE, ANTHROPIC_ACCOUNT, key)
        except keyring.errors.KeyringError as exc:
            raise CredentialError(f"Anahtar kaydedilemedi: {exc.__class__.__name__}",
                                  "Anahtar Windows Credential Manager'a kaydedilemedi.") from exc
        log.info("Claude API anahtarı güvenli depoya kaydedildi (%s)", self.backend_name())

    def delete_anthropic_key(self) -> None:
        try:
            keyring.delete_password(SERVICE, ANTHROPIC_ACCOUNT)
            log.info("Claude API anahtarı silindi")
        except keyring.errors.PasswordDeleteError:
            pass
