#!/usr/bin/env python3
"""TVCBOOK 完整播放器直链采集器。Python 3.10+；login/浏览器回退需 Playwright。"""
from __future__ import annotations

import argparse
import ast
import csv
import email.utils
import http.client
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LIST = ('https://www.tvcbook.com/s/videos.html?q=&sort=&fos=frames&i=&t=8644'
                '&l=&v=&c=&d=3&p=&b=&se=&ch=&y=&page=1')
DEFAULT_SESSION = '.tvcbook/session.json'


def log(message):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {message}', flush=True)


def atomic_json(path, value, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    # 登录状态在写入前就限制权限，避免临时文件暴露会话信息。
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    os.replace(temp, path)
    if private:
        path.chmod(0o600)


def playwright_api():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError as exc:
        raise RuntimeError('请先安装：python -m pip install playwright；python -m playwright install chromium') from exc


def login(args):
    with playwright_api()() as pw:
        browser = pw.chromium.launch(headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto('https://www.tvcbook.com/login', wait_until='domcontentloaded', timeout=60000)
            print('请在弹出的浏览器中自行登录、处理验证码和协议。不要关闭浏览器。')
            input('登录成功后，回到终端按 Enter 保存登录状态：')
            if urllib.parse.urlsplit(page.url).path.startswith('/login'):
                raise RuntimeError('页面仍处于登录地址；请重新运行 login 并完成登录。')
            state = context.storage_state()
            state['collector_user_agent'] = page.evaluate('navigator.userAgent')
            atomic_json(args.session, state, private=True)
            log(f'登录状态已保存至 {Path(args.session).resolve()}')
        finally:
            browser.close()


def json_variable(html, name):
    """只解码字符串字面量及 JSON，不执行网页 JavaScript。"""
    pattern = (r'\b(?:var|let|const)\s+' + re.escape(name)
               + r"\s*=\s*JSON\.parse\(\s*'((?:\\.|[^'\\])*)'\s*\)")
    match = re.search(pattern, html)
    if not match:
        raise ValueError(f'详情/列表源码中未找到 {name}')
    return json.loads(ast.literal_eval("'" + match.group(1) + "'"))


def player_url(html):
    value = json_variable(html, 'handle_video')
    if not isinstance(value, str):
        raise ValueError('handle_video 不是字符串')
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname:
        raise ValueError('播放器地址为空或不是直接 HTTP(S) 地址')
    # 保留完整签名路径和查询参数，不重组、猜测或截断。
    return value


def page_url(source, number):
    parsed = urllib.parse.urlsplit(source)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    pairs = [(key, value) for key, value in pairs if key != 'page']
    pairs.append(('page', str(number)))
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(pairs), fragment=''))


def list_items(html, number):
    payload = json_variable(html, 'datas')
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError('列表 datas.data 结构变化，已停止，避免采错记录')
    items = []
    for position, item in enumerate(data, 1):
        video_id = str(item.get('id', ''))
        if not video_id.isdigit():
            raise ValueError('列表中出现无效视频编号')
        media = item.get('media') or {}
        items.append({
            'title': item.get('title', ''),
            'duration_seconds': media.get('duration'),
            'detail_url': f'https://www.tvcbook.com/video/{video_id}.html',
            'list_page': number,
            'list_position': position,
        })
    return items


def backoff_seconds(headers, now=None):
    """优先尊重 Retry-After，其次读取站点限流重置头。"""
    now = time.time() if now is None else now
    server_now = now
    try:
        server_now = email.utils.parsedate_to_datetime(headers.get('Date', '')).timestamp()
    except (TypeError, ValueError, AttributeError):
        pass
    retry = headers.get('Retry-After')
    if retry:
        try:
            return max(2.0, float(retry) + 2)
        except ValueError:
            try:
                deadline = email.utils.parsedate_to_datetime(retry).timestamp()
                return max(2.0, deadline - server_now + 2)
            except (TypeError, ValueError, AttributeError):
                pass
    try:
        reset = float(headers.get('X-Rate-Limit-Reset', ''))
        # 本次站点返回的是 Unix 时间戳；也兼容较小的相对秒数。
        return max(2.0, reset - server_now + 2 if reset >= 1_000_000_000 else reset + 2)
    except (TypeError, ValueError):
        return 65.0


class Gate:
    def __init__(self, interval, path):
        self.interval, self.path = interval, Path(path)
        self.last_monotonic = None
        self.last_wall = float(self.path.read_text()) if self.path.exists() else 0

    def wait(self):
        elapsed = (time.monotonic() - self.last_monotonic if self.last_monotonic is not None
                   else time.time() - self.last_wall)
        time.sleep(max(0, self.interval - elapsed))
        self.last_monotonic = time.monotonic()
        self.path.write_text(str(time.time()))


class Client:
    def __init__(self, state, gate, timeout, retries):
        self.state, self.gate, self.timeout, self.retries = state, gate, timeout, retries
        self.ua = state.get('collector_user_agent', 'Mozilla/5.0')
        jar = http.cookiejar.CookieJar()
        for cookie in state.get('cookies', []):
            domain = cookie['domain']
            expires = cookie.get('expires', -1)
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=cookie['name'], value=cookie['value'], port=None,
                port_specified=False, domain=domain, domain_specified=domain.startswith('.'),
                domain_initial_dot=domain.startswith('.'), path=cookie.get('path', '/'),
                path_specified=True, secure=cookie.get('secure', False),
                expires=int(expires) if expires > 0 else None, discard=expires <= 0,
                comment=None, comment_url=None, rest={'HttpOnly': cookie.get('httpOnly', False)}))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.pw = self.browser = self.context = self.page = None

    def fetch(self, url):
        for attempt in range(1, self.retries + 1):
            self.gate.wait()
            try:
                request = urllib.request.Request(url, headers={'User-Agent': self.ua})
                with self.opener.open(request, timeout=self.timeout) as response:
                    if urllib.parse.urlsplit(response.url).path.startswith('/login'):
                        raise RuntimeError('登录失效：请重新运行 login，再用原 collect 命令续采。')
                    html = response.read().decode('utf-8')
                    if re.search(r'<title>\s*登录', html):
                        raise RuntimeError('收到登录页：请重新运行 login。')
                    return html
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    delay = backoff_seconds(exc.headers)
                    log(f'429 限流；等待 {delay:.1f} 秒，再重试同一页面（{attempt}/{self.retries}）')
                elif exc.code in (500, 502, 503, 504):
                    delay = 15
                    log(f'服务器 {exc.code}；稍后重试（{attempt}/{self.retries}）')
                else:
                    raise RuntimeError(f'详情/列表页返回 HTTP {exc.code}：{url}') from exc
            except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as exc:
                delay = 15
                log(f'网络错误 {type(exc).__name__}；稍后重试（{attempt}/{self.retries}）')
            if attempt < self.retries:
                time.sleep(delay)
        raise RuntimeError(f'重试耗尽，已保留断点：{url}')

    def browser_url(self, url):
        """页面字段变化时，使用同一登录状态读取实际 video DOM。"""
        if self.page is None:
            self.pw = playwright_api()().start()
            self.browser = self.pw.chromium.launch(headless=True)
            storage = {key: self.state.get(key, [])
                       for key in ('cookies', 'origins')}
            self.context = self.browser.new_context(storage_state=storage, user_agent=self.ua)
            self.page = self.context.new_page()
        for attempt in range(1, self.retries + 1):
            self.gate.wait()  # 浏览器回退是额外一次详情页请求，也必须限速。
            response = self.page.goto(url, wait_until='domcontentloaded', timeout=self.timeout * 1000)
            if response and response.status == 429:
                delay = backoff_seconds(response.headers)
                log(f'浏览器回退收到 429，等待 {delay:.1f} 秒')
                if attempt < self.retries:
                    time.sleep(delay)
                continue
            if response and response.status >= 400:
                raise RuntimeError(f'浏览器详情页返回 HTTP {response.status}')
            if urllib.parse.urlsplit(self.page.url).path.startswith('/login'):
                raise RuntimeError('浏览器登录状态失效，请重新运行 login')
            self.page.wait_for_function('''() => {
                const v = document.querySelector('video');
                return v && (v.currentSrc || v.getAttribute('src') || v.querySelector('source')?.src);
            }''', timeout=self.timeout * 1000)
            result = self.page.locator('video').first.evaluate('''v =>
                v.currentSrc || v.getAttribute('src') || v.querySelector('source')?.src''')
            if not isinstance(result, str) or urllib.parse.urlsplit(result).scheme not in ('http', 'https'):
                raise ValueError('播放器尚未给出可保存的 HTTP(S) 地址')
            return result
        raise RuntimeError('浏览器回退重试耗尽')

    def close(self):
        if self.browser:
            self.browser.close()
        if self.pw:
            self.pw.stop()


def read_records(path):
    records = {}
    if not path.exists():
        return records
    lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
    valid = []
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index != len(lines) - 1:
                raise RuntimeError('results.jsonl 中间有损坏记录，请先备份并检查')
            log('忽略并移除意外中断留下的不完整末行')
            path.write_text(''.join(valid), encoding='utf-8')
            break
        records[record['detail_url']] = record
        valid.append(line if line.endswith('\n') else line + '\n')
    else:
        if lines and not lines[-1].endswith('\n'):
            path.write_text(''.join(valid), encoding='utf-8')
    return records


def export(out, candidates, records, limit):
    rows = [records[c['detail_url']] for c in candidates[:limit] if c['detail_url'] in records]
    successful = [row for row in rows if row.get('media_url')]
    atomic_json(out / 'videos.json', rows)
    (out / 'urls.txt').write_text(''.join(row['media_url'] + '\n' for row in successful), encoding='utf-8')
    columns = ['rank', 'title', 'duration_seconds', 'list_page', 'list_position', 'detail_url',
               'media_url', 'extraction_method', 'captured_at', 'error']
    with (out / 'videos.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    return len(successful)


def collect(args):
    source = page_url(args.list_url, 1)
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme != 'https' or parsed.hostname != 'www.tvcbook.com' or parsed.path != '/s/videos.html':
        raise ValueError('--list-url 必须是 https://www.tvcbook.com/s/videos.html 的筛选列表地址')
    state = json.loads(Path(args.session).read_text(encoding='utf-8'))
    if not state.get('cookies'):
        raise ValueError('登录状态中没有 Cookie，请先运行 login')
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    lock = out / '.collect.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f'{out} 已有采集锁。请确认没有其他进程运行；异常退出后可手动删除 {lock}。') from exc
    os.close(fd)
    client = None
    candidates, records = [], {}
    checkpoint = out / 'results.jsonl'
    try:
        snapshot_path = out / 'candidates.json'
        snapshot = (json.loads(snapshot_path.read_text(encoding='utf-8')) if snapshot_path.exists()
                    else {'source_list_url': source, 'next_page': 1, 'candidates': []})
        if snapshot['source_list_url'] != source:
            raise ValueError('本输出目录已用于其他筛选条件，请更换 --output 目录')
        candidates = snapshot['candidates']
        records = read_records(checkpoint)
        gate = Gate(args.interval, out / '.last_request')
        client = Client(state, gate, args.timeout, args.retries)
        while len(candidates) < args.limit:
            number = snapshot['next_page']
            html = client.fetch(page_url(source, number))
            items = list_items(html, number)
            if not items:
                log('站点列表已结束')
                break
            seen = {item['detail_url'] for item in candidates}
            for item in items:
                if item['detail_url'] in seen:
                    raise RuntimeError('分页出现重复作品，可能列表已变化；已停止，避免改变既定顺序')
                item['rank'] = len(candidates) + 1
                candidates.append(item)
                seen.add(item['detail_url'])
            snapshot['next_page'] = number + 1
            atomic_json(snapshot_path, snapshot)
            log(f'列表第 {number} 页已保存；候选 {len(candidates)} 条')
        for candidate in candidates[:args.limit]:
            previous = records.get(candidate['detail_url'], {})
            if previous.get('media_url'):
                continue
            record = dict(candidate)
            try:
                html = client.fetch(candidate['detail_url'])
                try:
                    url, method = player_url(html), 'detail_page.handle_video'
                except (ValueError, SyntaxError, TypeError):
                    if args.no_browser_fallback:
                        raise
                    log(f'第 {candidate["rank"]} 条字段未读到，尝试浏览器 video.currentSrc')
                    url, method = client.browser_url(candidate['detail_url']), 'video.currentSrc/src/source'
                record.update(media_url=url, extraction_method=method,
                              captured_at=datetime.now(timezone.utc).isoformat())
            except RuntimeError:
                raise  # 登录、限流重试耗尽等问题停止整个任务，避免继续发无效请求。
            except Exception as exc:
                record.update(error=f'{type(exc).__name__}: {exc}',
                              captured_at=datetime.now(timezone.utc).isoformat())
                log(f'第 {candidate["rank"]} 条未采到地址，已记入 error；下次运行会重试')
            with checkpoint.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            records[record['detail_url']] = record
            done = export(out, candidates, records, args.limit)
            log(f'已取得 {done}/{min(args.limit, len(candidates))} 条；当前序号 {candidate["rank"]}')
        missing = sum(not records.get(c['detail_url'], {}).get('media_url') for c in candidates[:args.limit])
        log(f'本轮结束，未成功 {missing} 条。再次运行相同命令可续采/重试。')
        return 2 if missing or len(candidates) < args.limit else 0
    finally:
        try:
            if candidates:
                done = export(out, candidates, records, args.limit)
                log(f'已导出 {done} 条直链至 {out.resolve()}')
        finally:
            try:
                if client:
                    client.close()
            finally:
                lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('login', help='打开浏览器，手动登录并保存状态')
    command.add_argument('--session', default=DEFAULT_SESSION)
    command = commands.add_parser('collect', help='按固定列表顺序采集完整直链，可断点续采')
    command.add_argument('--session', default=DEFAULT_SESSION)
    command.add_argument('--list-url', default=DEFAULT_LIST)
    command.add_argument('--limit', type=int, default=1000)
    command.add_argument('--interval', type=float, default=10.4, help='请求开始间隔，最小 10.4 秒')
    command.add_argument('--output', default='tvcbook_result')
    command.add_argument('--timeout', type=int, default=30)
    command.add_argument('--retries', type=int, default=5)
    command.add_argument('--no-browser-fallback', action='store_true')
    args = parser.parse_args()
    if args.command == 'collect' and (args.limit < 1 or args.interval < 10.4 or args.timeout < 1 or args.retries < 1):
        parser.error('limit、timeout、retries 必须为正数；interval 至少为 10.4')
    try:
        if args.command == 'login':
            login(args)
            return 0
        return collect(args)
    except KeyboardInterrupt:
        log('已停止；已完成记录保留。')
        return 130
    except Exception as exc:
        log(f'停止：{exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
