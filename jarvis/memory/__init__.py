"""Uzun süreli hafıza (Aşama 4).

- `store`: SQLite'ta kayıt, düzenleme, silme, süre dolumu, tekrar tespiti
- `retrieval`: soruya göre ilgili kayıtları bulma (FTS5 + önem + yakınlık + kullanım)
- `extractor`: konuşmadan hatırlanacak bilgiyi seçme (önce yerel süzgeç, sonra Haiku)
- `summarizer`: biten konuşmaları kısa özet olarak saklama
- `service`: hepsini sohbet akışına bağlayan katman

Kısa süreli bağlam (son mesajlar) `core/conversation.py`'dedir; burası ondan ayrıdır.
"""
