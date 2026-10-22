"""本机状态接口：把 host.collect() 的结果交给网页。

只读，没有任何写操作入口。将来要加关机 / 卸载盘，新增函数时必须自己再做一次
本机会话校验 —— 不要指望这一层替你做。
"""


def send_system(handler, collect):
    try:
        data = collect()
    except Exception as e:                     # 采集失败要说清楚，不能给半张表
        handler.send_json({'error': '系统状态读取失败: %s' % e}, 500)
        return
    handler.send_json(data)
