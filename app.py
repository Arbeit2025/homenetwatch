"""homenetwatch（おうちネットワーク見える化）

自宅PC上でローカル実行する前提の単一ファイル Flask アプリ。
実行マシンが所属するサブネット（/24）を自動推定し、その範囲内だけを
スキャンして、到達可能なホストと主要ポートの開放状況を表で表示する。

安全設計:
- スキャン対象は「実行マシン自身の所属サブネット」だけに固定する。
  ユーザーが範囲を指定する入力欄は無く、外部のネットワークは走査できない。
- DB は使わない。結果はメモリ上だけに保持し、外部へ送信しない。
"""

from __future__ import annotations

import ipaddress
import platform
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

# 開放確認する主要ポート（よくある家庭内サービスの代表）
CHECK_PORTS: List[int] = [22, 80, 443, 445, 3389]
PORT_TIMEOUT = 0.5  # 秒
MAX_WORKERS = 32

JST = ZoneInfo("Asia/Tokyo")


# ============================
# ネットワーク推定
# ============================
def get_local_ip() -> str:
    """実行マシンの LAN 側 IPv4 アドレスを返す（取得できなければ 127.0.0.1）。

    UDP ソケットを外部アドレスへ「接続」（実際には何も送信しない）すると、
    OS が選んだ送信元アドレスから LAN 側 IP が分かる、という定番の方法を使う。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        pass
    finally:
        sock.close()
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def guess_subnet(ip: str) -> str:
    """IPv4 アドレスから所属サブネット（/24）の文字列表現を返す。"""
    network = ipaddress.ip_network(f"{ip}/24", strict=False)
    return str(network)


def current_subnet() -> str:
    """実行マシンの所属サブネットを返す。"""
    return guess_subnet(get_local_ip())


# ============================
# 到達確認・ポート確認（テストでモック可能な単位に分離）
# ============================
def ping_host(ip: str, timeout: int = 1) -> bool:
    """ping を1回打ち、応答があれば True。プラットフォーム差異を吸収する。"""
    if platform.system().lower().startswith("win"):
        cmd = ["ping", "-n", "1", "-w", str(timeout * 1000), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(timeout), ip]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def check_port(ip: str, port: int, timeout: float = PORT_TIMEOUT) -> bool:
    """TCP 接続が成立すれば True（ポート開放）。"""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_hostname(ip: str) -> str:
    """逆引きでホスト名を引く。引けなければ空文字。"""
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return ""


# ============================
# スキャン本体
# ============================
def scan_host(ip: str) -> Dict:
    """1ホスト分を調べる。ping 応答かポート開放のどちらかがあれば到達とみなす。"""
    open_ports = [p for p in CHECK_PORTS if check_port(ip, p)]
    alive = bool(open_ports) or ping_host(ip)
    hostname = resolve_hostname(ip) if alive else ""
    return {
        "ip": ip,
        "alive": alive,
        "open_ports": open_ports,
        "hostname": hostname,
    }


def scan_network(subnet: str) -> List[Dict]:
    """指定サブネット内のホストを走査し、到達できたものだけを IP 順で返す。"""
    network = ipaddress.ip_network(subnet)
    hosts = [str(h) for h in network.hosts()]
    found: List[Dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for result in executor.map(scan_host, hosts):
            if result["alive"]:
                found.append(result)
    found.sort(key=lambda r: ipaddress.ip_address(r["ip"]))
    return found


def run_scan() -> Dict:
    """実行マシンの所属サブネットだけをスキャンし、結果を辞書で返す。

    対象サブネットはここで自動推定したものに固定され、
    リクエスト側から範囲を指定する方法は無い。
    """
    subnet = current_subnet()
    hosts = scan_network(subnet)
    return {
        "subnet": subnet,
        "scanned_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "host_count": len(hosts),
        "hosts": hosts,
    }


# ============================
# 画面・API
# ============================
INDEX_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>homenetwatch｜おうちネットワーク見える化</title>
<style>
  body { font-family: sans-serif; margin: 2em auto; max-width: 880px; padding: 0 1em; }
  table { border-collapse: collapse; width: 100%; margin-top: 1em; }
  th, td { border: 1px solid #ccc; padding: 0.4em 0.6em; text-align: left; }
  th { background: #f0f4f8; }
  button { padding: 0.6em 1.4em; font-size: 1em; cursor: pointer; }
  .note { color: #555; font-size: 0.9em; }
</style>
</head>
<body>
<h1>🏠 homenetwatch（おうちネットワーク見える化）</h1>
<p>このPCが所属するサブネット: <strong>{{ subnet }}</strong></p>
<p class="note">スキャン範囲は、このPCの所属サブネット内に限定されます。他のネットワークを指定することはできません。</p>
<button id="scan-btn" type="button">スキャン開始</button>
<span id="status"></span>
<div id="result"></div>
<script>
const btn = document.getElementById('scan-btn');
const status = document.getElementById('status');
const result = document.getElementById('result');
btn.addEventListener('click', async () => {
  btn.disabled = true;
  status.textContent = 'スキャン中…（最大で1分ほどかかることがあります）';
  result.innerHTML = '';
  try {
    const res = await fetch('/api/scan', {method: 'POST'});
    const data = await res.json();
    status.textContent = '完了: ' + data.scanned_at + ' ／ 見つかった機器 ' + data.host_count + ' 台';
    let html = '<table><thead><tr><th>IPアドレス</th><th>ホスト名</th><th>到達</th><th>開いているポート</th></tr></thead><tbody>';
    for (const h of data.hosts) {
      html += '<tr><td>' + h.ip + '</td><td>' + (h.hostname || '―') + '</td><td>○</td><td>' +
        (h.open_ports.length ? h.open_ports.join(', ') : '（なし）') + '</td></tr>';
    }
    html += '</tbody></table>';
    result.innerHTML = html;
  } catch (e) {
    status.textContent = 'スキャンに失敗しました: ' + e;
  } finally {
    btn.disabled = false;
  }
});
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML, subnet=current_subnet())


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    return jsonify(run_scan())


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
