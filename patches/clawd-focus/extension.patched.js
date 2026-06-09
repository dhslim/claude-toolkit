const vscode = require("vscode");
const http = require("http");

// Port range for Clawd terminal-focus extension instances.
// Each editor window gets its own extension host → each needs a unique port.
// main.js broadcasts to all ports; only the one with the matching PID responds 200.
const PORT_BASE = 23456;
const PORT_RANGE = 44; // ports 23456-23499 (bumped from 5 - claude-toolkit\patches\clawd-focus; app.asar broadcast bumped to match)

let server = null;
let boundPort = null;

// [clawd-focus-window-raise patch v4 - added by claude-toolkit\patches\clawd-focus]
// VS Code multiplexes many project windows under ONE Code.exe PID, so Clawd's
// desktop-app focus lands on the wrong window. The extension instance that
// matched the terminal is ALREADY in the correct window, so we raise that window
// ourselves by matching the workspace folder basename against the Code window
// titles. v4: a PERSISTENT PowerShell helper compiles the Win32 focus code ONCE
// and stays warm, so each raise is ~instant (no per-click spawn/compile) - the
// correction lands before Clawd's wrong-window focus can render, killing the flash.
var raiseHelper = null;

function clawdLog(m) {
  try {
    var fs = require("fs"), os = require("os"), path = require("path");
    fs.appendFileSync(path.join(os.tmpdir(), "clawd-focus-debug.log"), new Date().toISOString() + " [ext] " + m + "\n");
  } catch (e) {}
}

function buildHelperScript() {
  return [
    '$ErrorActionPreference="SilentlyContinue"',
    'try{ [Console]::InputEncoding=[System.Text.Encoding]::UTF8 }catch{}',
    'Add-Type -TypeDefinition @"',
    'using System;using System.Text;using System.Runtime.InteropServices;',
    'public class ClawdRaise {',
    ' public delegate bool E(IntPtr h,IntPtr l);',
    ' [DllImport("user32.dll")] public static extern bool EnumWindows(E cb,IntPtr l);',
    ' [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);',
    ' [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr h);',
    ' [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h,StringBuilder s,int n);',
    ' [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h,out uint pid);',
    ' [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();',
    ' [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);',
    ' [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);',
    ' [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int n);',
    ' [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a,uint b,bool f);',
    ' [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();',
    ' public static IntPtr Find(string sub){ IntPtr found=IntPtr.Zero; EnumWindows((h,l)=>{ if(IsWindowVisible(h)){ int len=GetWindowTextLength(h); if(len>0){ StringBuilder sb=new StringBuilder(len+1); GetWindowText(h,sb,sb.Capacity); if(sb.ToString().IndexOf(sub,StringComparison.OrdinalIgnoreCase)>=0){ found=h; return false; } } } return true; },IntPtr.Zero); return found; }',
    ' public static void Raise(string sub){ IntPtr h=Find(sub); if(h==IntPtr.Zero) return; IntPtr fg=GetForegroundWindow(); uint p; uint ft=GetWindowThreadProcessId(fg,out p); uint cur=GetCurrentThreadId(); AttachThreadInput(cur,ft,true); ShowWindow(h,9); BringWindowToTop(h); SetForegroundWindow(h); AttachThreadInput(cur,ft,false); }',
    '}',
    '"@',
    '$in=[Console]::In',
    'while($true){ $t=$in.ReadLine(); if($t -eq $null){ break }; $t=$t.Trim(); if($t.Length -gt 0){ try{ [ClawdRaise]::Raise($t) }catch{} } }',
  ].join("\n");
}

function getRaiseHelper() {
  if (raiseHelper && !raiseHelper.killed && raiseHelper.stdin && raiseHelper.stdin.writable) return raiseHelper;
  try {
    var cp = require("child_process");
    var psExe = (process.env.SystemRoot || "C:\\Windows") + "\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";
    var b64 = Buffer.from(buildHelperScript(), "utf16le").toString("base64");
    var child = cp.spawn(psExe, ["-NoProfile", "-NonInteractive", "-EncodedCommand", b64], { windowsHide: true, stdio: ["pipe", "ignore", "ignore"] });
    child.on("exit", function () { if (raiseHelper === child) raiseHelper = null; });
    child.on("error", function () { if (raiseHelper === child) raiseHelper = null; });
    if (child.stdin) child.stdin.on("error", function () {});
    child.unref();
    raiseHelper = child;
    clawdLog("helper spawned pid=" + child.pid);
    return child;
  } catch (e) { clawdLog("helper spawn FAIL: " + (e && e.message)); return null; }
}

function raiseOwnWindow() {
  try {
    if (process.platform !== "win32") return;
    var folders = vscode.workspace.workspaceFolders;
    if (!folders || !folders.length) return;
    var name = ((folders[0].uri && folders[0].uri.fsPath) || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop();
    if (!name) return;
    var helper = getRaiseHelper();
    if (helper && helper.stdin && helper.stdin.writable) {
      helper.stdin.write(name + "\n");
      clawdLog("raise -> [" + name + "]");
    }
  } catch (e) { clawdLog("raiseOwnWindow exception: " + (e && e.message)); }
}

async function focusTerminalByPids(pids) {
  for (const terminal of vscode.window.terminals) {
    const termPid = await terminal.processId;
    if (termPid && pids.includes(termPid)) {
      terminal.show(false);
      raiseOwnWindow(); // [clawd-focus-window-raise patch]
      return true;
    }
  }
  return false;
}

function tryListen(port, maxPort) {
  if (port > maxPort) {
    console.log("Clawd terminal-focus: all ports in use, HTTP server disabled");
    return;
  }

  server = http.createServer((req, res) => {
    if (req.method === "POST" && req.url === "/focus-tab") {
      let body = "";
      req.on("data", (chunk) => { body += chunk; });
      req.on("end", () => {
        try {
          const data = JSON.parse(body);
          const pids = Array.isArray(data.pids) ? data.pids.filter(Number.isFinite) : [];
          if (pids.length) {
            focusTerminalByPids(pids).then((found) => {
              res.writeHead(found ? 200 : 404);
              res.end(found ? "ok" : "not found");
            });
          } else {
            res.writeHead(400);
            res.end("no pids");
          }
        } catch {
          res.writeHead(400);
          res.end("bad json");
        }
      });
    } else {
      res.writeHead(404);
      res.end();
    }
  });

  server.on("error", (err) => {
    if (err.code === "EADDRINUSE") {
      server = null;
      tryListen(port + 1, maxPort);
    }
  });

  server.listen(port, "127.0.0.1", () => {
    boundPort = port;
    console.log(`Clawd terminal-focus: listening on 127.0.0.1:${port}`);
  });
}

function activate(context) {
  tryListen(PORT_BASE, PORT_BASE + PORT_RANGE - 1);

  // Warm the focus helper at startup so even the first "Jump to Terminal" is
  // instant (no compile-on-first-click flash).
  if (process.platform === "win32") { try { getRaiseHelper(); } catch (e) {} }

  // URI handler kept as fallback for manual testing:
  // vscode://clawd.clawd-terminal-focus?pids=1234,5678
  context.subscriptions.push(
    vscode.window.registerUriHandler({
      async handleUri(uri) {
        const params = new URLSearchParams(uri.query);
        const raw = params.get("pids") || params.get("pid") || "";
        const pids = raw.split(",").map(Number).filter(Boolean);
        if (pids.length) focusTerminalByPids(pids);
      },
    })
  );
}

function deactivate() {
  if (server) {
    server.close();
    server = null;
  }
  if (raiseHelper) {
    try { raiseHelper.stdin.end(); } catch (e) {}
    try { raiseHelper.kill(); } catch (e) {}
    raiseHelper = null;
  }
}

module.exports = { activate, deactivate };
