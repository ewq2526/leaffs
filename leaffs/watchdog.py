# -*- coding: utf-8 -*-
"""冻结看门狗 —— 疑似服务卡死时自动把全线程栈写入 config/freeze_dump_*.txt。

历史：原定义于 leaffs.py，重构抽出；leaffs.py 通过
from leaffs.watchdog import _wd_start, _wd_finish, _wd_ws_tick, _wd_loop 使用。

机制：有 HTTP 请求在途或 WS 近期活跃，且连续 15s 无任何请求/消息完成，
即视为疑似卡死，自动 dump 全部线程调用栈。仅诊断用、开销可忽略
（1 线程每秒醒一次）；长流下载期间无其它请求时可能误报一次（冷却 120s），
文件头会注明现场数据供人工判断。
"""
import os
import threading
import time

from leaffs.paths import CONFIG_DIR
from leaffs.runtime_log import add_log

_wd_lock = threading.Lock()
_wd_inflight = 0        # 在途 HTTP 请求数（线程内 handle 开始 +1 / 结束 -1）
_wd_last_done = 0.0     # 最近一次请求完成时刻（time.monotonic）
_wd_ws_last = 0.0       # 最近一次 WS 消息到达时刻（事件循环活性信号）
_wd_last_dump = 0.0     # 最近一次自动 dump 时刻（冷却用）


def _wd_start():
    global _wd_inflight
    with _wd_lock:
        _wd_inflight += 1


def _wd_finish():
    global _wd_inflight, _wd_last_done
    with _wd_lock:
        _wd_inflight = max(0, _wd_inflight - 1)
        _wd_last_done = time.monotonic()


def _wd_ws_tick():
    """WS 每条消息到达时更新活性（事件循环是否还在转）"""
    global _wd_ws_last
    _wd_ws_last = time.monotonic()


def _wd_loop():
    import faulthandler
    while True:
        time.sleep(1.0)
        try:
            now = time.monotonic()
            with _wd_lock:
                ws_alive_recently = _wd_ws_last > 0 and now - _wd_ws_last < 120
                busy = _wd_inflight > 0 or ws_alive_recently
                idle = now - max(_wd_last_done, _wd_ws_last)
            if busy and idle >= 15 and now - _wd_last_dump >= 120:
                _wd_last_dump = now
                try:
                    p = os.path.join(CONFIG_DIR,
                                     'freeze_dump_' + time.strftime('%Y%m%d_%H%M%S') + '.txt')
                    with open(p, 'w', encoding='utf-8') as f:
                        f.write(f'[watchdog] 在途请求 {_wd_inflight} 条且 {idle:.0f}s 无完成，'
                                f'疑似卡死，时间 {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
                        faulthandler.dump_traceback(file=f, all_threads=True)
                    add_log('看门狗: 疑似服务卡死，全线程栈已写入 ' + os.path.basename(p))
                except Exception:
                    pass
        except Exception:
            pass
