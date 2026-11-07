"""Değişmeyen sabitler.

Model kimlikleri burada yalnızca *varsayılan* olarak durur; kullanıcı
ayarlardan değiştirebilir. Kimlikler resmi Anthropic dokümantasyonundan
doğrulanmıştır (2026-11). Fiyatlar Aşama 2'de model kayıt defterine taşınacak.
"""

DEFAULT_MODEL_LIGHT = "claude-haiku-5-5"
DEFAULT_MODEL_MAIN = "claude-sonnet-5-5"
DEFAULT_MODEL_ADVANCED = "claude-opus-5-5"

# Sistem izleme örnekleme aralığı (saniye)
SYSTEM_SAMPLE_INTERVAL_S = 1.0
# Sparkline için tutulacak örnek sayısı
SYSTEM_HISTORY_LEN = 60
