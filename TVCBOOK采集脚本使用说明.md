# TVCBOOK 播放器直链采集脚本使用说明

脚本：`tvcbook_collect.py`。适用于 Python 3.10 及以上版本。

用途：登录 TVCBOOK 后，按筛选列表顺序取得播放器的完整直链，并保存直链与视频详情页的对应关系。默认请求间隔为 **10.4 秒**，目标为前 **1,000 条**。输出的是地址列表，不是完整视频文件。

## 1. 安装

将脚本放入一个文件夹，在该文件夹打开终端。macOS / Linux 执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install playwright
python -m playwright install chromium
```

Windows PowerShell 中，虚拟环境的激活命令改为：

```powershell
.venv\Scripts\Activate.ps1
```

后续示例中的 `python` 均指已经激活的虚拟环境。

## 2. 登录

```bash
python tvcbook_collect.py login
```

1. 脚本打开一个 Chromium 浏览器窗口。
2. 在窗口中自行填写账号、密码，处理协议、验证码等步骤。
3. 确认已进入登录后的页面。先保留浏览器窗口，回到终端按 Enter。
4. 脚本保存登录状态，然后关闭该窗口。

默认登录状态路径是当前目录下的 `.tvcbook/session.json`。脚本不内置手机号或密码；该文件包含可用于登录的会话信息，请留在本机，不要分享或提交到代码仓库。

如果仍停留在 `/login` 地址，脚本会提示重新登录。保存成功也不代表会话永久有效；实际采集时若跳回登录页，程序会停止并提示重新执行 `login`。

## 3. 先试采 3 条

```bash
python tvcbook_collect.py collect --limit 3 --output demo_result
```

默认使用这次任务的筛选条件：广告片，站点时长筛选 `d=3`（界面显示约 30 秒至 1 分钟），从第一页开始。

脚本先保存列表候选，再访问详情页。列表每页目前是 24 条，因此试采时可能显示“候选 24 条”，但只采集前 3 条详情页。

## 4. 正式采集前 1,000 条

```bash
python tvcbook_collect.py collect --limit 1000 --interval 10.4 --output tvcbook_result
```

`--interval` 是程序主动发起的列表或详情页请求之间的最小开始间隔，单位为秒。默认值为 `10.4`，脚本只接受不小于此值的设置。例如，需要更慢时可设为 `15`。

程序会先固定本次列表顺序，再逐条读取详情页。每个页面原本直接提供的完整播放器地址都会原样保存，包括签名路径和查询参数。

## 5. 输出文件

默认保存在 `tvcbook_result/`：

| 文件 | 内容 |
| --- | --- |
| `urls.txt` | 已取得的完整直链，一行一条，只包含采集成功的地址 |
| `videos.csv` | CSV（Comma-Separated Values，逗号分隔值）明细，含标题、详情页、直链、页码、采集时间等；可用 Excel 打开 |
| `videos.json` | JSON（JavaScript Object Notation）格式的明细，保留采集失败记录的 `error` 字段 |
| `results.jsonl` | JSON Lines（逐行 JSON）检查点，每取得一条结果即追加并落盘，用于断点续采 |
| `candidates.json` | 本次列表的候选顺序、筛选地址及分页进度 |
| `.last_request` | 上次请求开始时间，用于重启后继续控制间隔 |
| `.collect.lock` | 运行锁，防止两个进程同时写入同一个结果目录；正常退出时自动清除 |

主要明细字段：

| 字段 | 含义 |
| --- | --- |
| `rank` | 在本次固定列表中的序号 |
| `title` | 视频标题 |
| `duration_seconds` | 列表数据提供的秒数，可能含小数；与界面整数时长可能略有差异 |
| `list_page` / `list_position` | 原列表页码、页内位置 |
| `detail_url` | TVCBOOK 正常视频详情页，适合在浏览器中打开测试 |
| `media_url` | 完整播放器直链 |
| `extraction_method` | 地址来自详情页 `handle_video`，还是浏览器播放器元素 |
| `captured_at` | 采集时间，采用 UTC（Coordinated Universal Time，协调世界时） |
| `error` | 本次未取得地址的原因；采集成功时为空 |

文件导出会随着每条结果更新。`urls.txt` 行数表示已经取得的地址数量；候选数量或 CSV 行数不能单独当作成功数量。

## 6. 停止和断点续采

在运行中的终端按 **Ctrl+C**。脚本会保留检查点，并导出已经完成的结果。

恢复时，在相同工作目录中重新运行原命令：

```bash
python tvcbook_collect.py collect --limit 1000 --interval 10.4 --output tvcbook_result
```

程序会跳过已经保存了 `media_url` 的记录，继续未完成部分；有 `error` 的记录会再次尝试。

请保留整个结果目录。改变筛选条件时，使用另一个 `--output` 目录。不要同时运行多个采集进程；它们可能共享站点的限流额度。

如果电脑强制关机留下 `.collect.lock`，先确认原采集进程已经结束，再手动删除这个锁文件，然后执行原命令。不要删除 `results.jsonl` 和 `candidates.json`。

重新登录不会自动重新采集已经保存的直链。若需要更新全部签名地址，使用一个新的结果目录重新采集。

## 7. 更换筛选条件

在网站中调整筛选条件，复制 `/s/videos.html` 的完整地址，通过 `--list-url` 传入。地址中含 `&`，必须用引号包住：

```bash
python tvcbook_collect.py collect \
  --list-url 'https://www.tvcbook.com/s/videos.html?q=&sort=&fos=frames&i=&t=8644&l=&v=&c=&d=3&p=&b=&se=&ch=&y=&page=1' \
  --limit 1000 \
  --interval 10.4 \
  --output another_result
```

上面的多行写法适用于 macOS / Linux；其他终端可以把它合并成一行。脚本总是从第一页开始，并保留其他筛选参数。

## 8. 采集方式

登录使用 Playwright 浏览器。批量采集优先使用 HTTP（Hypertext Transfer Protocol，超文本传输协议）读取已登录页面：

1. 从列表页 `datas` 数据取得视频编号、标题和详情页对应关系。
2. 实际访问每一条详情页，解码页面直接传给播放器的 `handle_video` 完整地址。
3. 如果该字段未读到，使用浏览器打开同一详情页，等待并读取：

   ```javascript
   const video = document.querySelector('video');
   video?.currentSrc ||
     video?.getAttribute('src') ||
     video?.querySelector('source')?.src;
   ```

详情页地址可以依据列表中的视频编号构造；**播放器直链始终从实际页面读取，不能由视频编号推算**。脚本只解析源码中的字符串和 JSON，不执行从源码捕获的 JavaScript。

浏览器回退会增加一次详情页访问，同样先等待请求间隔。需要关闭这项回退时，可增加 `--no-browser-fallback`；页面字段未读到的条目会记录错误，留待后续重试。

## 9. 限流、耗时和常见问题

### 为什么选择 10.4 秒

本次采集中曾收到 `429 Too Many Requests`，响应头显示 `X-Rate-Limit-Limit: 6`。结合额度耗尽、重置时间及连续请求表现，采用了约每分钟 6 次的保守策略：

```text
60 ÷ 6 = 10 秒，再加 0.4 秒余量
```

这是客户端的保守配置，不代表已经确认网站对所有用户、账号或网络都执行相同规则，也不保证永远不会触发 429。

程序遇到 429 时：优先使用 `Retry-After`；其次使用 `X-Rate-Limit-Reset`；无法识别时默认等待 65 秒。等待后重试同一页。网络超时、传输中断及部分服务器错误也会重试；默认最多尝试 5 次，耗尽后停止并保留断点。

### 采集 1,000 条需要多久

仅 1,000 次详情页请求的固定间隔就约为 **173 分钟**。首次还需读取约 42 个列表页，通常合计约 **3 小时**，另加登录、限流退避、网络重试及浏览器回退时间。

### 直链访问返回 403

这个脚本的默认任务是取得地址，**不对视频服务器批量做可播放性验证**，因此导出的明细没有视频响应状态列。取得 `media_url` 表示页面提供了这个地址，不代表已经确认视频一定能播放或下载。

此前采集时，一部分视频直链返回 `403`，响应头同时显示签名校验成功、存储类型为 `DeepColdArchive`（深度冷归档）。它们与详情页请求过快产生的 `429` 是不同问题。仅凭这些响应头不能完全确定正常界面是否也会失败；可通过对应 `detail_url` 测试网站内的播放和下载。

### 地址可能过期

完整地址包含站点生成的签名信息，应原样使用。采集和续采会保留此前地址，不能据此保证旧签名持续有效。需要新地址时，可使用新结果目录重新采集。

### 网站页面结构变化

脚本针对这次观察到的 `datas`、`handle_video` 和 `<video>` 结构编写。若列表结构改变，程序会停止；如果分页出现重复作品，也会停止，避免悄悄改变既定顺序。候选列表分多次请求取得，不是网站提供的事务快照。

## 10. 本次交付的验证范围

已使用保存的真实列表、详情页样本验证提取；同时离线测试了完整查询参数保留、429 等待时间计算、10.4 秒间隔、检查点末行损坏处理，以及中断后续采。此次整理没有重新启动网站批量采集；新登录会话需按本文步骤在你的浏览器中完成。

这套脚本首次运行会创建自己的检查点目录。此前已交付的 538 条采集结果仍单独保留，没有包含在本脚本的新任务检查点中。
