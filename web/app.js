const state = {
  pollTimer: null,
  runtime: {},
};

const els = {
  urlsText: document.getElementById("urlsText"),
  downloadMode: document.getElementById("downloadMode"),
  modeNotice: document.getElementById("modeNotice"),
  notesBox: document.getElementById("notesBox"),
  outputDir: document.getElementById("outputDir"),
  manifestPath: document.getElementById("manifestPath"),
  authorFields: document.getElementById("authorFields"),
  authorUrl: document.getElementById("authorUrl"),
  downloadRange: document.getElementById("downloadRange"),
  dateRangeFields: document.getElementById("dateRangeFields"),
  dateStart: document.getElementById("dateStart"),
  dateEnd: document.getElementById("dateEnd"),
  browserName: document.getElementById("browserName"),
  browserProfile: document.getElementById("browserProfile"),
  retries: document.getElementById("retries"),
  timeout: document.getElementById("timeout"),
  overwrite: document.getElementById("overwrite"),
  statusText: document.getElementById("statusText"),
  progressText: document.getElementById("progressText"),
  progressFill: document.getElementById("progressFill"),
  logText: document.getElementById("logText"),
  summaryBox: document.getElementById("summaryBox"),
  runtimeBox: document.getElementById("runtimeBox"),
  startBtn: document.getElementById("startBtn"),
  pauseBtn: document.getElementById("pauseBtn"),
  cancelBtn: document.getElementById("cancelBtn"),
  loadUrlsBtn: document.getElementById("loadUrlsBtn"),
  saveUrlsBtn: document.getElementById("saveUrlsBtn"),
};

const MODE_CONTENT = {
  video: {
    notice:
      "适合抖音分享页、抖音分享文案、普通视频帖子。目标是尽量拿到可获得的最高视频画质。",
    notes: [
      "1. 左侧可以直接粘贴完整的抖音分享文案，不必自己手动提取 URL。",
      "2. 默认先试“不读取浏览器 Cookie”；如果分享页解析失败，再切到你已登录抖音的浏览器。",
      "3. 最高画质通常需要 ffmpeg 合并音视频，这个工具已经在本地接好了。",
      "4. 这一模式下载的是视频，不负责把图文帖子拆成整组图片。",
    ].join("\n"),
  },
  douyin_media: {
    notice:
      "适合抖音图文作品、图集作品、单张动图或多张动图连在一起的作品。这个模式会先抓图片 / 动图资源，再尽量额外生成一个 MP4。",
    notes: [
      "1. 这个模式直接吃抖音分享页或分享文案，不需要你手动提取直链。",
      "2. 静态图会优先尝试无水印图片源；如果你是为了刷新以前下过的旧文件，再手动勾选“覆盖同名文件”。",
      "3. 默认会识别已经下载过的同一作品，并跳过重复下载。",
      "4. 如果作品里有单张动图或多张动图资源，会优先保存原资源，并尽量合成或转成 MP4。",
      "5. 如果作品里只有静态图，也会额外生成一个幻灯片 MP4，方便预览或二次整理。",
      "6. 配乐能拿到时会单独保存；预览 MP4 生成失败时，原始图片 / 动图文件仍然会保留。",
    ].join("\n"),
  },
  douyin_author_auto: {
    notice:
      "适合直接贴某个抖音作者主页链接。程序会先抓作者作品列表，再自动识别每条作品是视频还是图文 / 动图，并分流到对应下载器。",
    notes: [
      "1. 这个模式不需要你手工整理单条作品链接，直接填写作者主页链接或作者分享短链即可。",
      "2. 作品列表接口通常更依赖登录态，建议优先选择你已经登录抖音网页版的浏览器 Cookie。",
      "3. 同一个作者主页批量抓取时，程序会优先按作品 ID 去重，并记住断点，下次会从上次停下的位置继续。",
      "4. 图文 / 动图作品会走抖音图文模式；普通视频作品会走 yt-dlp 视频模式。",
      "5. 如果作者列表接口漏掉一部分作品，程序会自动弹出浏览器扫描作者主页，补齐遗漏的作品 ID 再下载；首次启用时可能需要在浏览器里登录。",
      "6. 如果你只想刷新某个作者以前下过的旧文件，再手动勾选“覆盖同名文件”。",
    ].join("\n"),
  },
  image: {
    notice:
      "当前这个实验模式接的是 gallery-dl。上游目前支持 TikTok 图文链接，但不支持抖音分享页 / v.douyin.com 这类链接。",
    notes: [
      "1. 这个模式只适合 TikTok 图文帖子，例如 /photo/ 形式的链接。",
      "2. 如果你粘贴的是抖音分享页，程序会直接提示不支持，而不是假装开始下载。",
      "3. 图文模式默认只导出图片，不会顺带把配乐音频一起下回来。",
      "4. 遇到私密、地区限制或需要登录态的帖子，可以切到已登录的浏览器 Cookie 再试。",
    ].join("\n"),
  },
};

function payloadFromForm() {
  return {
    urls_text: els.urlsText.value,
    download_mode: els.downloadMode.value,
    output_dir: els.outputDir.value,
    manifest_path: els.manifestPath.value,
    author_url: els.authorUrl.value,
    download_range: els.downloadRange.value,
    date_start: els.dateStart.value,
    date_end: els.dateEnd.value,
    browser_name: els.browserName.value,
    browser_profile: els.browserProfile.value,
    retries: Number(els.retries.value || 3),
    timeout: Number(els.timeout.value || 60),
    overwrite: els.overwrite.checked,
  };
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "请求失败");
  }
  return data;
}

function setRunning(running) {
  els.startBtn.disabled = running;
  els.startBtn.style.display = running ? "none" : "";
  els.pauseBtn.style.display = running ? "" : "none";
  els.cancelBtn.style.display = running ? "" : "none";
  els.loadUrlsBtn.disabled = running;
  els.saveUrlsBtn.disabled = running;
  els.downloadMode.disabled = running;
  if (!running) {
    els.pauseBtn.textContent = "暂停";
  }
}

function updateProgress(current, total) {
  const safeTotal = total || 0;
  els.progressText.textContent = `${current} / ${safeTotal}`;
  const percent = safeTotal > 0 ? Math.min((current / safeTotal) * 100, 100) : 0;
  els.progressFill.style.width = `${percent}%`;
}

function renderSummary(snapshot) {
  els.summaryBox.className = "summary";
  if (snapshot.last_error) {
    els.summaryBox.classList.add("error");
    els.summaryBox.textContent = `失败：${snapshot.last_error}`;
    return;
  }
  if (snapshot.summary) {
    els.summaryBox.classList.add("success");
    const s = snapshot.summary;
    els.summaryBox.textContent = `完成：成功 ${s.ok || 0}，跳过 ${s.skipped || 0}，失败 ${s.failed || 0}。`;
    return;
  }
  if (snapshot.current_url) {
    els.summaryBox.textContent = `当前处理：${snapshot.current_url}`;
    return;
  }
  els.summaryBox.textContent = "";
}

function renderState(snapshot) {
  els.statusText.textContent = snapshot.status || "就绪";
  if (snapshot.running && snapshot.download_mode) {
    els.downloadMode.value = snapshot.download_mode;
    els.authorUrl.value = snapshot.author_url;
    els.downloadRange.value = snapshot.download_range || "all";
    els.dateStart.value = snapshot.date_start || "";
    els.dateEnd.value = snapshot.date_end || "";
    els.dateRangeFields.style.display = els.downloadRange.value === "date_range" ? "" : "none";
  }
  updateProgress(snapshot.current || 0, snapshot.total || 0);
  els.logText.value = (snapshot.logs || []).join("\n");
  els.logText.scrollTop = els.logText.scrollHeight;
  renderSummary(snapshot);
  renderRuntime(snapshot.runtime || {});
  renderModeUi(snapshot.runtime || {}, els.downloadMode.value, els.browserName.value);
  setRunning(Boolean(snapshot.running));
  els.pauseBtn.textContent = snapshot.paused ? "恢复" : "暂停";
}

function renderRuntime(runtime) {
  state.runtime = runtime;
  const videoVersion = runtime.video?.yt_dlp_version || "unknown";
  const ffmpeg = runtime.video?.ffmpeg_path || "未找到";
  const videoBrowsers = (runtime.video?.supported_browsers || []).join(" / ") || "无";
  const douyinBrowsers = (runtime.douyin_media?.supported_browsers || []).join(" / ") || "无";
  const imageVersion = runtime.tiktok_image?.gallery_dl_version || "unknown";
  const imageBrowsers = (runtime.tiktok_image?.supported_browsers || []).join(" / ") || "无";
  els.runtimeBox.textContent = [
    `视频模式: yt-dlp ${videoVersion}`,
    `ffmpeg: ${ffmpeg}`,
    `视频模式浏览器支持: ${videoBrowsers}`,
    `抖音图文 / 动图模式浏览器支持: ${douyinBrowsers}`,
    `TikTok 图文模式: gallery-dl ${imageVersion}`,
    `TikTok 图文模式浏览器支持: ${imageBrowsers}`,
  ].join("\n");
}

function browserListForMode(runtime, mode) {
  if (mode === "douyin_media") {
    return runtime.douyin_media?.supported_browsers || [];
  }
  if (mode === "image") {
    return runtime.tiktok_image?.supported_browsers || [];
  }
  return runtime.video?.supported_browsers || [];
}

function renderBrowserOptions(runtime, mode, selected) {
  const options = ["none", ...browserListForMode(runtime, mode)];
  const finalSelected = options.includes(selected) ? selected : "none";
  els.browserName.innerHTML = "";
  for (const name of options) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name === "none" ? "不读取浏览器 Cookie" : name;
    if (name === finalSelected) option.selected = true;
    els.browserName.appendChild(option);
  }
}

function renderModeUi(runtime, mode, selectedBrowser) {
  const content = MODE_CONTENT[mode] || MODE_CONTENT.video;
  els.modeNotice.textContent = content.notice;
  els.notesBox.value = content.notes;
  els.authorFields.style.display = mode === "douyin_author_auto" ? "grid" : "none";
  renderBrowserOptions(runtime, mode, selectedBrowser);
}

async function loadDefaults() {
  const defaults = await requestJson("/api/defaults");
  els.urlsText.value = defaults.urls_text || "";
  els.downloadMode.value = defaults.download_mode || "video";
  els.outputDir.value = defaults.output_dir || "";
  els.manifestPath.value = defaults.manifest_path || "";
  els.authorUrl.value = defaults.author_url || "";
  els.downloadRange.value = defaults.download_range || "all";
  els.dateStart.value = defaults.date_start || "";
  els.dateEnd.value = defaults.date_end || "";
  els.dateRangeFields.style.display = els.downloadRange.value === "date_range" ? "" : "none";
  els.browserProfile.value = defaults.browser_profile || "";
  els.retries.value = defaults.retries || 3;
  els.timeout.value = defaults.timeout || 60;
  els.overwrite.checked = Boolean(defaults.overwrite);
  renderRuntime(defaults.runtime || {});
  renderModeUi(defaults.runtime || {}, els.downloadMode.value, defaults.browser_name || "none");
}

async function refreshState() {
  const snapshot = await requestJson("/api/state");
  renderState(snapshot);
}

async function startDownload() {
  try {
    setRunning(true);
    await requestJson("/api/start", {
      method: "POST",
      body: JSON.stringify(payloadFromForm()),
    });
    await refreshState();
  } catch (error) {
    setRunning(false);
    alert(error.message);
  }
}

async function pauseResume() {
  const isPaused = els.pauseBtn.textContent === "恢复";
  const endpoint = isPaused ? "/api/resume" : "/api/pause";
  try {
    await requestJson(endpoint, { method: "POST" });
    await refreshState();
  } catch (error) {
    alert(error.message);
  }
}

async function cancelDownload() {
  if (!confirm("确定要取消下载吗？当前文件完成后会停止。")) return;
  try {
    await requestJson("/api/cancel", { method: "POST" });
    await refreshState();
  } catch (error) {
    alert(error.message);
  }
}

async function saveUrls() {
  try {
    await requestJson("/api/save-urls", {
      method: "POST",
      body: JSON.stringify({ urls_text: els.urlsText.value }),
    });
    alert("已保存到 urls.txt");
  } catch (error) {
    alert(error.message);
  }
}

async function init() {
  await loadDefaults();
  await refreshState();
  state.pollTimer = window.setInterval(() => {
    refreshState().catch((error) => {
      els.summaryBox.className = "summary error";
      els.summaryBox.textContent = error.message;
    });
  }, 1200);

  els.startBtn.addEventListener("click", startDownload);
  els.pauseBtn.addEventListener("click", pauseResume);
  els.cancelBtn.addEventListener("click", cancelDownload);
  els.loadUrlsBtn.addEventListener("click", loadDefaults);
  els.saveUrlsBtn.addEventListener("click", saveUrls);
  els.downloadMode.addEventListener("change", () => {
    renderModeUi(state.runtime || {}, els.downloadMode.value, els.browserName.value);
  });
  els.downloadRange.addEventListener("change", () => {
    els.dateRangeFields.style.display = els.downloadRange.value === "date_range" ? "" : "none";
  });
}

init().catch((error) => {
  els.summaryBox.className = "summary error";
  els.summaryBox.textContent = error.message;
});
