"""Türkçe dışındaki diller için isteğe bağlı Piper sesleri ("bunu İngilizce söyle").

Türkçe ses (tr_TR-dfki) İngilizce kelimeleri Türkçe okur. İngilizce bir cümle söylenecekse
ilk ihtiyaçta İngilizce bir erkek ses indirilir (~61 MB, yalnızca bir kez) ve o cümle onunla
seslendirilir. Ayarlar ekranındaki ses listesine eklenmez: asıl ses Türkçe kalır.

Seçilen ses: `en_US-norman-medium` (Hugging Face `rhasspy/piper-voices`, sabit `v1.0.0`).
* Model kartı: veri kümesi LibriVox kayıtları, lisans **kamu malı (public domain)**; model sıfırdan
  eğitilmiş (başka bir sesten ince ayar değil). Kart: en/en_US/norman/medium/MODEL_CARD.
* Diğer adaylar elendi: ryan ve hfc_male veri kümeleri CC BY-NC-SA (ticari olmayan); joe, alan,
  kusal, northern_english_male gibi sesler lessac (Blizzard 2013, araştırma lisansı) sesinden ince
  ayarla üretilmiş.
* Boyut ve md5 değerleri `voices.json` ile aynı; dosyalar indirilip özetleri doğrulandı.
"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.voice.models import PIPER_BASE, ModelFile


@dataclass(frozen=True)
class LanguageVoice:
    language: str          # "en"
    key: str               # Piper ses adı
    title: str             # kullanıcıya gösterilen ad
    license: str
    onnx: ModelFile
    config: ModelFile

    @property
    def files(self) -> tuple[ModelFile, ModelFile]:
        return self.onnx, self.config

    @property
    def size_mb(self) -> int:
        return round((self.onnx.size or 0) / 1_048_576)


def _voice(language: str, key: str, title: str, license_: str, onnx_size: int, onnx_md5: str, json_size: int,
           json_md5: str) -> LanguageVoice:
    lang, rest = key.split("-", 1)
    name, quality = rest.rsplit("-", 1)
    base = f"{PIPER_BASE}/{lang.split('_')[0]}/{lang}/{name}/{quality}/{key}"
    rel = f"piper/{key}"
    return LanguageVoice(
        language, key, title, license_,
        ModelFile(f"piper-{key}", f"{rel}.onnx", f"{base}.onnx?download=true", title, onnx_size, md5=onnx_md5),
        ModelFile(f"piper-{key}-json", f"{rel}.onnx.json", f"{base}.onnx.json?download=true", f"{title} ayarı",
                  json_size, md5=json_md5, kind="json"))


ENGLISH_VOICE = _voice("en", "en_US-norman-medium", "İngilizce ses (norman, erkek)", "public domain (LibriVox)",
                       63531379, "829cea515dc724d694b83b71e8083f9f", 4968, "975830d6f230f6eccf657d265de99eba")

LANGUAGE_VOICES: dict[str, LanguageVoice] = {"en": ENGLISH_VOICE}
LANGUAGE_NAMES = {"tr": "Türkçe", "en": "İngilizce"}
