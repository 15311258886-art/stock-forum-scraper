"""viewer 包：可视化查看器（本地 Web 服务 + 数据库 + 接入层）。

工程化收口说明（2026-10-22，P0-05）：
  · ``import viewer.server`` 现在**没有任何模块顶层副作用**——不再写库、不拉调度线程、
    不预热。所有启动期副作用（ensure_schema + 调度 + 预热）已收口到 ``boot()``，仅由
    ``main()`` / desktop_app 在真正启动服务时调用。这样 13 个外部工具 ``import viewer.server``
    不再变成生产库写者（此前 import 即触发建表 + 抓取线程，是重大隐患）。
  · 启动服务：
        import viewer.server as s
        s.boot()                         # 收口后的启动副作用（幂等，含 schema 迁移）
        s.create_server().serve_forever()
    或直接 ``python -m viewer.server``（``__main__`` 会依次调用 boot + serve）。
  · 写接口令牌（P1-45）：经 main() 启动默认启用随机令牌，落盘于 config/viewer_token.txt；
    设置面板经 /api/config/get、/api/config/set 读写（P1-50 / P1-51）。
"""
