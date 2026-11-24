# Daily Research & Industry Radar (`daily-radar`)

面向一线科研与工程追踪的**每日 10:00（北京时间）飞书早报卡片 + 独立项目级 GitHub Pages 三级多页面静态情报站**。

## 核心设计准则

1. **降噪优先于求全（5–10 分钟总览）**：每日总览页严格控制在 **5–10 张精选摘要卡**（只展示中英双标题、方向/机构徽章、45–60 字痛点一句话、1–2 个实测指标徽章、开源与 4–8 卡门槛徽章），其余 **10–20 条泛相关动态**默认折叠为单行列表；某板块当日无高优工作时如实留空不凑数。
2. **四要素直白拆解 + 原文数字核验**：单篇详情子页统一按「① 真实痛点与翻车场景 → ② 具体方法机制 → ③ 实验基准数字与对比口径（自动比对原文，未直接匹配的数字显式打标 `⚠️ 原文待核`）→ ④ 公开技术参考与复现成本」四要素展开，支持一键复制 `BibTeX` 与 `zoteroctl` 预览入库命令，退回总览页精准恢复原滚动位置。
3. **十年历史留存与零二进制安全红线**：Git 主分支仅提交每日结构化 JSON（`content/YYYY/MM/YYYY-MM-DD.json`），由 GitHub Actions 在云端动态构建三级多页面 HTML、外置指纹 `CSS/JS` 与按年分片检索索引（`/data/index-YYYY.json`），推送前强制通过零二进制与密钥/内网信息扫描。

## 目录结构

- `config/profile.yaml`：双档画像（P0 核心课题 `GUI-RL` 与 `OSSL/OOD` + 标杆实验室名单；P1 多模态与 RL 工程底座批次相对打分）
- `config/sources.yaml`：声明式信源清单（arXiv API、HF Daily Papers、GitHub Trending、核心框架 GitHub Release、官方实验室博客与精选个人通讯，内置时效窗口过滤、`[AINews]` 自动拆条与连续 3 天坏源告警）
- `config/conferences.json`：2026–2027 国际学术会议与专题 Workshop 日历（按 CCF 第七版标注，放榜窗口自动开启特刊横幅）
- `daily_radar/`：抓取、7 天滑动窗口去重、主备双通道 LLM 提炼、原文数字核验、密钥扫描、三级静态站渲染与飞书 Webhook 签名推送模块
- `content/YYYY/MM/YYYY-MM-DD.json`：每日结构化源数据
- `.github/workflows/deploy-pages.yml`：GitHub Actions 自动构建与 `actions/deploy-pages` 部署工作流
- `systemd/`：用户级定时调度单元（`OnCalendar=*-*-* 09:15:00 Asia/Shanghai` 启动构建校验，`10:00:00` 准点发飞书卡片）

## 快速开始与本地验证

```bash
cd ~/workspace/daily-radar

# 1. 初始化本机 0600 凭据目录 (~/.config/daily-radar/{webhook.json,llm.json,sensitive_terms.txt})
python3 -m daily_radar init-config

# 2. 录入新建飞书群自定义机器人的 Webhook URL 与 HMAC-SHA256 签名密钥（输入隐藏，权限 0600，并立即推送验证卡）
python3 -m daily_radar configure-webhook --verify-send

# 3. 构建三级多页面静态站点至 _site/ 并执行公开安全扫描
python3 -m daily_radar build-site --content-dir content --config-dir config --out-dir _site
python3 -m daily_radar scan-secrets --target .
python3 -m daily_radar scan-secrets --target _site

# 4. 预览飞书 JSON 2.0 早报卡片载荷与字节体积（< 20 KB）
python3 -m daily_radar send-feishu content/2026/11/2026-11-24.json --dry-run

# 5. 本地启动 HTTP 预览服务器
python3 -m daily_radar serve-local --port 8765

# 6. 录入详情页复制的偏好反馈口令（沉淀近 60 天正负样本用于打分校准）
python3 -m daily_radar record-feedback "#雷达反馈 👍 2026-11-24/gui-rl-2611-04073 [gui-rl]"

# 7. 运行完整自动化测试套件
python3 -m unittest discover -s tests -v
```

## 启用 GitHub Pages 独立仓库、专用飞书群与定时任务

1. **一键创建公开仓库并启用 GitHub Actions Pages**（`phylipppp-zzy/daily-radar` -> `https://phylipppp-zzy.github.io/daily-radar/`）：

```bash
bash scripts/bootstrap_github_pages.sh
```

2. **新建专用飞书群并接入自定义机器人 Webhook**（与日常 Agent 对话流物理隔离）：
   - 在飞书客户端点击「新建群组」（例如命名为 `每日科研雷达`）→ 打开右上角「群设置 → 群机器人 → 添加机器人 → 自定义机器人」；
   - 安全设置勾选 **「签名校验」**，复制生成的 **Webhook 地址** 与 **签名密钥（Secret）**；
   - 在终端执行下列命令（输入全程隐藏，写入 `0600` 权限 `~/.config/daily-radar/webhook.json`，并立即向新群发送今日验证早报卡）：

```bash
python3 -m daily_radar configure-webhook --verify-send
```

3. **安装并启用用户级 `systemd` 定时器**（每日 `09:15 CST` 构建部署并轮询 Pages 就绪，`10:00 CST` 准点推送飞书早报卡）：

```bash
bash systemd/install_units.sh --enable
```
