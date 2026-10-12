// 启动器：内部设置 CHROME_PATH / NODE_PATH，再用当前 node 拉起 verify_e2e.js。
// 这样 Bash 侧只需执行一个绝对路径命令，避免 shell 变量赋值触发权限解析问题。
const fs = require("fs");
const cp = require("child_process");

const HOME = "C:/Users/野比&大熊";
process.env.CHROME_PATH = HOME + "/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe";
process.env.NODE_PATH = HOME + "/.workbuddy/binaries/node/workspace/node_modules";

const ROOT = "F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper";
const node = process.execPath;
const script = ROOT + "/verify_e2e.js";
const logPath = ROOT + "/ui_test.txt";

const logFd = fs.openSync(logPath, "w");
const child = cp.spawn(node, [script], { stdio: ["ignore", logFd, logFd], env: process.env });
child.on("close", (code) => {
  fs.writeSync(logFd, "\n==== node exit code: " + code + " ====\n");
  fs.closeSync(logFd);
  process.exit(code || 0);
});
