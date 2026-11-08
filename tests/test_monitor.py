import time

from jarvis.system.monitor import SystemMonitor


def test_sample_returns_real_sane_values():
    m = SystemMonitor()
    try:
        a = m.sample()
        time.sleep(0.2)  # Windows'ta saat çözünürlüğü ~15 ms: hız için gerçek bir aralık gerekir
        b = m.sample()
    finally:
        m.close()
    assert 0 <= a.cpu_percent <= 100
    assert a.ram_total_gb > 0 and 0 < a.ram_used_gb <= a.ram_total_gb
    assert a.disk_total_gb > 0 and 0 <= a.disk_free_gb <= a.disk_total_gb
    assert a.net_down_bps is None          # ilk örnekte hız bilinemez
    assert b.net_down_bps is not None and b.net_down_bps >= 0
    assert a.uptime_s > 0


def test_gpu_missing_is_reported_not_faked():
    m = SystemMonitor()
    try:
        snap = m.sample()
    finally:
        m.close()
    if snap.gpu is None:
        assert snap.gpu_unavailable_reason  # neden açıkça belirtilmeli
    else:
        assert snap.gpu.name
