# 王者荣耀热点营销工作台

Apify 是唯一的在线采样来源，覆盖抖音和小红书。管理员通过 GitHub Actions 的 Sample and deploy Pages 手动运行，勾选 apify_trends。每轮各取近 7 天的高互动样本，并发布话题榜和审核队列。需要仓库 Secret APIFY_TOKEN。普通代码推送只发布当前快照。

审核和草稿保存在浏览器。重置后的新队列使用独立存储，并在刷新时清除该网站上旧队列的存储。
