@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo  [股票论坛查看器] 本机一键回填 (雪球 + 摩尔)
echo  时间: %date% %time%
echo ============================================================

REM ---- 解释器 ----
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "NODE=C:\Users\野比&大熊\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
if not exist "%NODE%" set "NODE=node"

echo [解释器] PY=%PY%  NODE=%NODE%
echo.

REM ============================================================
echo [A] 全站回填：复用现有 ingest 链路（含雪球讨论/交易/帖子）
echo     注意：雪球/淘股吧需本机浏览器+代理(127.0.0.1:53269)；
echo           淘股吧还需先导入 Cookie，否则自动跳过。
echo ============================================================
"%PY%" run_backfill_local.py --sites eastmoney,sina,ths,taoguba,xueqiu
echo [A] 退出码 %errorlevel%
echo.

REM ============================================================
echo [B] 雪球组合持仓(cube)回填：需 xueqiu_targets.json（uid 清单）+ cookies.json
echo ============================================================
if exist cookies.json ( set "XQ_COOKIE_FILE=cookies.json" ) else if exist cookies.txt ( set "XQ_COOKIE_FILE=cookies.txt" )
if exist xueqiu_targets.json (
    echo    检测到 xueqiu_targets.json，开始抓取雪球组合持仓...
    "%NODE%" xueqiu_cube_crawler.cjs
    if exist xueqiu_cubes.json (
        "%PY%" xueqiu_cube_load.py
    ) else (
        echo    [B 跳过] 未生成 xueqiu_cubes.json（抓取可能被 WAF/未登录拦截，详见脚本控制台）
    )
) else (
    echo    [B 跳过] 未找到 xueqiu_targets.json（雪球 34 人 uid 清单）。
        echo             如需补雪球组合持仓：把 34 人 uid 清单写入 xueqiu_targets.json（JSON 数组），
        echo             并把雪球 Cookie 存为 cookies.json(JSON) 或 cookies.txt(Netscape) 放本目录，
        echo             用你之前那份到 10 月的 cookie 即可，无需重新登录。再重跑本 bat。
)
echo.

REM ============================================================
echo [C] 摩尔金融抓取（探查型）：需代理；可选 moer_targets.json
echo ============================================================
"%NODE%" moer_crawler.cjs
if exist moer_posts.json (
    "%PY%" moer_load.py
) else (
    echo    [C 跳过] 未生成 moer_posts.json（URL 不对/被拦截，详见脚本控制台）
)
echo.

REM ============================================================
echo [收尾] 重建选手池（让 B/C 新入库的雪球/摩尔选手进入查看器）
echo ============================================================
"%PY%" -c "import sys; sys.path.insert(0,'.'); from viewer.db import Database; from viewer.ingest import Ingestor; db=Database(); r=Ingestor(db).rebuild_player_pool(); print('POOL', r); db.close()"
echo.

echo ============================================================
echo [完成] 全流程结束。请关闭并重新打开桌面「股票论坛可视化查看器」(.lnk)
echo        以加载新数据。A 阶段日志见 run_backfill_local.log。
echo ============================================================
pause >nul
endlocal
