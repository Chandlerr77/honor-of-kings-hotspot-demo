# 王者荣耀热点营销工作台

GitHub Pages 展示 B 站综合热门与 TapTap 官方版本公告的采样快照，以及带原始来源和时效标记的玩法与版本专题。GitHub Actions 每小时约第 13、43 分钟采样并发布，可在仓库 Actions 手动运行。源站错误会显示在页面，已有快照在缓存可用时保留。审核和脚本保存在访客自己的浏览器。

## 发布

1. 把压缩包内文件放到 GitHub 仓库的 main 分支根目录，包含隐藏的 .github 文件夹。
2. 在 Settings → Pages → Build and deployment → Source 选择 GitHub Actions。
3. 在 Actions → Sample and deploy Pages 点击 Run workflow。部署成功后在该次运行的部署记录中打开站点。

如有可访问的 RSSHub 实例，可在仓库 Settings → Secrets and variables → Actions 的 Variables 中配置 HOTSPOT_POPULAR_RSS 或 HOTSPOT_KPL_RSS。填写 RSS 完整 URL。

本地启动方式见 Local.md。网站访问者可刷新已发布的快照，网站管理员可从 GitHub Actions 发起新的采样。

## 社媒关键词采样

每轮分别请求 B 站关键词结果和 TikHub 的抖音、小红书关键词搜索。每个平台取首屏候选，仅入库带可核验发布时间且在最近 7 天内的作品。抖音按返回的话题字段或完整标签文本标记 #王者荣耀，普通关键词命中另行标注。

在仓库 Settings → Secrets and variables → Actions 添加 Repository secret TIKHUB_API_KEY 后，抖音和小红书的采样任务才会发出请求。TikHub 可能按调用收费，首轮先手动触发一次验证。服务方未返回的播放、点赞、评论、分享或收藏显示“未返回”。播放次数可能包含重复播放，独立观看人数需要另有明确数据来源。B 站关键词接口如果拒绝请求，状态栏会显示结果。
