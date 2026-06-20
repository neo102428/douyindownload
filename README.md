# 抖音分享链接批量下载工具

这个工具现在支持三条下载链路，用于批量处理你已经得到作者授权的分享链接：

- 抖音视频：走 `yt-dlp`
- 抖音图文 / 动图：走抖音 `aweme detail` 接口，再保存图片 / 动图资源，并尽量额外生成 MP4
- TikTok 图文：走 `gallery-dl`

## 文件说明

- `downloader.py`：原来的直链下载与链接提取工具
- `douyin_ytdlp.py`：基于 yt-dlp 的抖音分享链接下载后端
- `gui.py`：启动本地网页界面
- `web/`：网页界面静态文件
- `urls.txt`：可选的本地链接清单，一行一个
- `manifest.csv`：每次运行后生成的结果清单
- `downloads/`：默认下载目录

## 要求

- Python 3.9+

## 中文网页界面

启动方式：

```bash
python3 gui.py
```

启动后终端会打印一个本地地址，例如 `http://127.0.0.1:8765`。  
浏览器打开后，把抖音分享文案或 `https://v.douyin.com/...` 短链一行一个粘贴到左侧大输入框里，然后选择你登录抖音的浏览器 Cookie 来源，再点“开始下载”。

注意：

- 可以粘贴“带分享文案的一整段文本”，工具会自动抽出其中的 URL
- 现在网页工具会把分享页交给 `yt-dlp` 处理，而不是再把它当成直链文件地址
- 默认建议先试“不读取浏览器 Cookie”
- 如果公开分享页解析失败，再切到 `safari`、`chrome` 等已登录抖音的浏览器
- 抖音图文 / 动图模式会优先保存原始图片、动图资源、配乐，并尽量额外生成一个 MP4
- TikTok 图文模式仍然是实验链路，适合 `/photo/` 形式的 TikTok 链接
- 目前抖音图文 / 动图模式已支持 `v.douyin.com` 分享短链、`share/slides` 图文页以及实况动图作品

如果你不想自动打开浏览器：

```bash
python3 gui.py --no-browser
```

## 命令行方式

下面这些命令行示例主要对应旧的直链下载器：

```bash
python3 downloader.py
```

指定输入文件和输出目录：

```bash
python3 downloader.py my_urls.txt -o archived_videos
```

需要请求头时：

```bash
python3 downloader.py \
  --referer "https://example.com" \
  --header "Authorization: Bearer <token>" \
  --cookie "sessionid=..." \
  urls.txt
```

覆盖同名文件：

```bash
python3 downloader.py --overwrite
```

## 说明

- 网页界面现在适用于你已经拿到作者授权的抖音分享链接
- 旧的 `downloader.py` 仍然适用于文件直链
- 结果会写入 `manifest.csv`
