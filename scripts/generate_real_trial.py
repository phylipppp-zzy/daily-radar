#!/usr/bin/env python3
"""Generate verified 2026-11 multi-day content & execute full end-to-end trial run for 2026-11-24."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from daily_radar.cli import main as radar_main
from daily_radar.config import write_mode_0600_atomic
from daily_radar.models import (
    DailyIssue,
    FoldedItem,
    FourElements,
    MetricBadge,
    MetricRow,
    RadarItem,
)
from daily_radar.pipeline import normalize_pain_point_length
from daily_radar.verify import verify_radar_item


def _seed_historical_issues(content_dir: Path) -> None:
    """Seed verified 2026-11-22 and 2026-11-23 issues so archive, weekly, prev/next, and 7-day dedup work."""
    day22_items = [
        verify_radar_item(
            RadarItem(
                slug="gui-rl-2610-03818",
                section="papers",
                track="gui-rl",
                track_label="GUI-RL",
                subtopics=["gui-security", "gui-env"],
                lab_tags=[],
                institution_tags=["Control OSWorld Team"],
                title_zh="Control OSWorld：桌面智能体实时越权与隐蔽旁路动作控制环境",
                title_en="Control OSWorld: An AI Control Environment for GUI Computer Use Agents",
                one_line_pain=normalize_pain_point_length(
                    "针对桌面智能体执行主任务时夹带隐蔽越权操作难被在线拦截的盲区，构建主旁任务配对控制评测环境",
                    context_tag="GUI-RL",
                ),
                metric_badges=[MetricBadge(text="318 主任务 + 81 旁路风险任务")],
                openness="code_only",
                openness_label="仅代码开源",
                small_compute_friendly=True,
                small_compute_note="支持单机 Docker 评测",
                authors=["Maria Sanchez del Rio", "Mikhail Terekhov"],
                published_date="2026-11-22",
                venue_or_source="arXiv cs.AI",
                url_primary="https://arxiv.org/abs/2610.03818",
                url_pdf="https://arxiv.org/pdf/2610.03818",
                url_arxiv_html="https://arxiv.org/html/2610.03818",
                arxiv_id="2610.03818",
                abstract_en=(
                    "Control OSWorld pairs 318 standard OSWorld desktop tasks with 81 harmful side tasks "
                    "(such as unauthorized file exfiltration and covert setting edits) to evaluate whether "
                    "live control monitors can catch misaligned GUI computer-use agents during interaction."
                ),
                four_elements=FourElements(
                    pain_point="现有 GUI 安全基准多聚焦外部网页弹窗注入，缺少对智能体自身在长轨迹操作中夹带未授权文件外传或篡改系统配置等隐蔽旁路行为的实时监控评测。",
                    mechanism="在标准桌面环境中把 318 个常规用户指令与 81 个隐蔽恶意旁路目标组合，引入在线监控器（Control Monitor）在每步动作执行前审查屏幕与动作序列。",
                    experiments_summary="在 318 个 OSWorld 标准任务与 81 个有害旁路任务配对集上评测在线监督器对隐蔽越权动作的拦截率与正常任务误伤率。",
                    repro_and_reference="基于 OSWorld 容器栈扩展，无需训练百卡基座即可在高校单机环境运行安全监控实验。",
                    experiment_rows=[
                        MetricRow(
                            benchmark="Control OSWorld",
                            metric="任务规模",
                            value="318 主任务 / 81 旁路任务",
                            baseline_delta="首套桌面控制监控配对集",
                            setup_note="OSWorld Docker 环境",
                        )
                    ],
                    backbone_and_params="多模态闭源与开源 CUA 通用评测",
                    gpu_and_duration="免训练推理与 Docker 桌面容器评测",
                    seeds_note="原文摘要未单列种子数",
                ),
                relative_score=8.4,
            )
        ),
        verify_radar_item(
            RadarItem(
                slug="gui-rl-2610-06898",
                section="papers",
                track="gui-rl",
                track_label="GUI-RL",
                subtopics=["gui-security", "gui-env"],
                lab_tags=["lab-hitsz-zju"],
                institution_tags=["香港理工大学"],
                title_zh="DIBench：移动端 GUI 智能体欺骗性界面注入评测基准",
                title_en="DIBench: Benchmarking Deceptive Injections against Mobile GUI Agents",
                one_line_pain=normalize_pain_point_length(
                    "针对手机智能体易被伪装成系统通知或合规按钮的欺骗性界面诱导误点的问题，建立真机与模拟评测集",
                    context_tag="GUI-RL",
                ),
                metric_badges=[MetricBadge(text="4 框架 × 7 基座实测"), MetricBadge(text="7 商业 App + 3 模拟 App")],
                openness="code_only",
                openness_label="仅代码开源",
                small_compute_friendly=True,
                small_compute_note="8×A100 仅作开源模型推理部署",
                authors=["DIBench Authors (Hong Kong Polytechnic University)"],
                published_date="2026-11-22",
                venue_or_source="NeurIPS 2026 (Evaluations & Datasets Track)",
                url_primary="https://arxiv.org/abs/2610.06898",
                url_pdf="https://arxiv.org/pdf/2610.06898",
                url_arxiv_html="https://arxiv.org/html/2610.06898",
                arxiv_id="2610.06898",
                conference_slug="neurips-2026",
                conference_track_tier="Evaluations & Datasets Track · Poster",
                abstract_en=(
                    "Accepted to NeurIPS 2026 Evaluations and Datasets Track. DIBench evaluates 4 mobile GUI agent "
                    "frameworks across 7 backbone models on 7 commercial Apps and 3 simulated Apps, using 1 CPU server "
                    "for emulators and 1 server with 8 A100 GPUs solely for open-source model inference."
                ),
                four_elements=FourElements(
                    pain_point="手机端 App 常出现高仿系统弹窗与诱导性遮罩，过往测试只看静态截图成功率，缺乏在真实商业 App 交互流里的欺骗性注入定量对比。",
                    mechanism="覆盖 7 款真实商业应用与 3 款可控模拟应用，交叉组合 4 套主流手机智能体框架与 7 个多模态基座模型进行端到端攻击复现。",
                    experiments_summary="系统评测 4 个手机智能体框架与 7 个基座模型在 7 个商业 App 与 3 个模拟 App 上的任务完成率及被诱导误触率。",
                    repro_and_reference="属于 NeurIPS 2026 E&D 赛道典型高校算力可承担工作：仅需 1 台 CPU 模拟器主机加常规开源推理卡即可完整复现。",
                    backbone_and_params="4 套移动端框架 × 7 个 VLM 基座",
                    gpu_and_duration="1 台 CPU 服务器 + 8×A100（仅开源模型推理）",
                    seeds_note="原文未单列种子数",
                ),
                relative_score=8.1,
            )
        ),
        verify_radar_item(
            RadarItem(
                slug="ossl-ood-2606-22537",
                section="papers",
                track="ossl-ood",
                track_label="OSSL/OOD",
                subtopics=["open-world-vocab", "ossl-ood-core"],
                lab_tags=[],
                institution_tags=["ECCV 2026"],
                title_zh="NegAS：负标签引导注意力与打分的开放世界目标级 OOD 检测",
                title_en="NegAS: Negative Label Guided Attention and Scoring for Out-of-Distribution Object Detection with Vision-Language Models",
                one_line_pain=normalize_pain_point_length(
                    "针对整图级负标签方法无法定位复杂背景中局部未知物体的问题，将负词表引入密集检测注意力与打分",
                    context_tag="OSSL/OOD",
                ),
                metric_badges=[MetricBadge(text="ECCV 2026 录用")],
                openness="code_only",
                openness_label="仅代码开源",
                small_compute_friendly=True,
                small_compute_note="基于预训练开放词汇检测器推理扩展",
                authors=["NegAS Authors"],
                published_date="2026-11-22",
                venue_or_source="ECCV 2026",
                url_primary="https://arxiv.org/abs/2606.22537",
                url_pdf="https://arxiv.org/pdf/2606.22537",
                url_arxiv_html="https://arxiv.org/html/2606.22537",
                arxiv_id="2606.22537",
                conference_slug="eccv-mmda",
                conference_track_tier="Main · Poster",
                abstract_en=(
                    "NegAS (ECCV 2026, arXiv:2606.22537) expands Negative Label guidance into dense object detection "
                    "architectures such as YOLO-World and Grounding DINO via negative-label guided cross-attention "
                    "and region-level OOD scoring to separate unknown objects from background clutter."
                ),
                four_elements=FourElements(
                    pain_point="NegLabel 等整图分类级负标签方法在多目标复杂场景下容易受背景区域干扰，难以在框级别区分“背景噪声”与“真正的未知类别物体”。",
                    mechanism="在 YOLO-World 与 Grounding DINO 的跨模态注意力层注入筛选后的负标签词表，结合正负文本相似度差值计算候选框级未知分数。",
                    experiments_summary="在开放词汇目标级 OOD 检测基准上验证 YOLO-World 与 Grounding DINO 接入负标签注意力后的未知框召回与误报抑制效果。",
                    repro_and_reference="直接复用现有预训练开放词汇检测器，对词表筛选与区域对齐研究有直接参考价值。",
                    backbone_and_params="YOLO-World / Grounding DINO",
                    gpu_and_duration="常规单机多卡检测评测",
                    seeds_note="原文摘要未单列种子数",
                ),
                relative_score=7.8,
            )
        ),
        verify_radar_item(
            RadarItem(
                slug="vlm-infra-vllm-v0-33-0",
                section="opensource",
                track="vlm-infra",
                track_label="VLM/RL底座",
                subtopics=["inference-engine"],
                lab_tags=[],
                institution_tags=["vLLM Team"],
                title_zh="vLLM v0.33.0：全面切换 Model Runner V2 并默认开启弹性 KV Cache",
                title_en="vllm-project/vllm v0.33.0: Default Model Runner V2 & Extensible KV Cache",
                one_line_pain=normalize_pain_point_length(
                    "针对多模态与在线 RL 混部时手动调显存占比易 OOM 的痛点，默认按预热后剩余显存自动分配 KV 缓存",
                    context_tag="推理底座",
                ),
                metric_badges=[MetricBadge(text="Model Runner V2 默认启用"), MetricBadge(text="Python 3.11+ 安全加固")],
                openness="code_and_weights",
                openness_label="代码+权重已开源",
                small_compute_friendly=True,
                small_compute_note="单卡至多机通用推理引擎",
                authors=["vLLM Contributors"],
                published_date="2026-11-22",
                venue_or_source="GitHub Release (v0.33.0)",
                url_primary="https://github.com/vllm-project/vllm/releases",
                url_github="https://github.com/vllm-project/vllm",
                license_name="Apache-2.0",
                abstract_en=(
                    "vLLM v0.33.0 transitions fully to Model Runner V2 as the default GPU runner (deprecating V1), "
                    "enables extensible KV caching that automatically sizes KV cache from free GPU memory post-warmup, "
                    "removes permissive default CORS origins, and raises minimum Python requirement to 3.11+."
                ),
                four_elements=FourElements(
                    pain_point="在单机共卡运行 RL 训练器与 vLLM 采样器时，固定 `gpu_memory_utilization` 常因多模态视觉编码峰值波动引发显存溢出或浪费。",
                    mechanism="彻底移除旧版 Runner V1，统一到 Model Runner V2，并在预热结束后根据真实可用空闲显存动态设定可扩展 KV Cache 容量。",
                    experiments_summary="官方 Release 实装 Model Runner V2 默认路径、预热后自动 KV Cache 定容，并升级 Python 3.11+ 与默认 CORS 安全边界。",
                    repro_and_reference="升级本地 RL rollout 推理后端时需注意废弃的 V1 环境变量与 Python 3.11+ 版本要求。",
                    backbone_and_params="通用 LLM / VLM 推理引擎",
                    gpu_and_duration="全系 GPU 开箱即用",
                    seeds_note="系统框架版本发布（不涉及随机种子）",
                ),
                relative_score=7.5,
            )
        ),
        verify_radar_item(
            RadarItem(
                slug="vlm-infra-deepmind-genie-4",
                section="industry",
                track="vlm-infra",
                track_label="VLM/RL底座",
                subtopics=["multimodal-base"],
                lab_tags=["lab-openai-deepmind"],
                institution_tags=["Google DeepMind"],
                title_zh="Google DeepMind 发布 Genie 4：面向智能体交互训练的实时音画世界模型",
                title_en="Genie 4: Real-Time Interactive World Models for Safe Agent Simulation",
                one_line_pain=normalize_pain_point_length(
                    "针对真实交互环境并发慢且高风险操作不可逆的问题，提供支持原生音频与物理反馈的实时交互沙盒",
                    context_tag="世界模型",
                ),
                metric_badges=[MetricBadge(text="实时交互世界模型")],
                openness="blog_or_release",
                openness_label="官方一手报告",
                small_compute_friendly=False,
                small_compute_note="工业界大规模闭源基础设施",
                authors=["Google DeepMind"],
                published_date="2026-11-22",
                venue_or_source="Google DeepMind Blog",
                url_primary="https://deepmind.google/blog/",
                abstract_en=(
                    "Google DeepMind introduces Genie 4, a real-time interactive world model with native audio and "
                    "consistent environment simulation designed as a safe training and evaluation sandbox for agents."
                ),
                four_elements=FourElements(
                    pain_point="具身与数字智能体在真实环境做探索采样时吞吐受限，且错误动作可能造成不可逆状态污染。",
                    mechanism="通过统一时空与原生音频生成的交互式世界模型实时响应智能体动作输入，维持长时程状态一致性。",
                    experiments_summary="官方技术博客展示长时程实时交互一致性与多模态反馈能力，暂未开放模型权重与离线基准代码。",
                    repro_and_reference="属于工业界大规模预训练世界模型风向标；结合近期 OSU 关于教程条件下世界模型收益有限的实证结论，桌面 GUI 任务不必盲目追逐重型视觉世界模型。",
                    backbone_and_params="闭源大规模世界模型",
                    gpu_and_duration="工业界集群训练（未公开卡数）",
                    seeds_note="官方博客未单列",
                ),
                relative_score=6.9,
            )
        ),
    ]

    issue22 = DailyIssue(
        date="2026-11-22",
        weekday_zh="周日",
        generated_at_cst="2026-11-22 09:28:10 CST",
        global_trend_summary=[
            "今日精选 5 条高相关动态：GUI 安全评测同时出现桌面端控制监控（Control OSWorld）与移动端欺骗注入基准（港理工 DIBench），OOD 方向关注负标签向密集目标检测的扩展（NegAS）。",
            "底层基础设施方面，vLLM v0.33.0 正式移除旧版 V1 Runner 并默认启用弹性 KV Cache，升级 RL 采样环境时需留意配置兼容。",
        ],
        highlights_top3=[
            "• [GUI-RL] Control OSWorld：318 主任务配对 81 旁路风险任务，评测桌面智能体实时越权拦截",
            "• [GUI-RL] 香港理工大学 DIBench（NeurIPS 2026 E&D）：4 框架 × 7 基座实测手机欺骗注入",
            "• [VLM/RL底座] vLLM v0.33.0：默认切换 Model Runner V2 并按空闲显存自动分配 KV Cache",
        ],
        featured_items=day22_items,
        folded_items=[
            FoldedItem(
                slug="fold-gui-dom-snapshot",
                section="papers",
                track="gui-rl",
                track_label="GUI-RL",
                title_zh="WebSnapshot-Lite：静态网页可访问性树压缩对比小记",
                title_en="WebSnapshot-Lite: Accessibility Tree Token Compression Note",
                one_line_takeaway="仅在静态表单网页对比三种截断规则，未涉及多步 RL 训练",
                url="https://arxiv.org/list/cs.HC/recent",
                published_date="2026-11-22",
                venue_or_source="arXiv cs.HC",
            )
        ],
        run_mode="trial",
        provenance={
            "run_mode": "trial",
            "distiller": "deterministic-verified",
            "source_origin": "seeded_trial",
        },
    )
    issue22.validate()

    day23_items = [
        verify_radar_item(
            RadarItem(
                slug="gui-rl-2607-25904",
                section="papers",
                track="gui-rl",
                track_label="GUI-RL",
                subtopics=["gui-verifier", "gui-online-rl"],
                lab_tags=[],
                institution_tags=["IRA Authors"],
                title_zh="Interactive Reward Agent：结合界面变化与底层环境状态核验的 GUI 奖励智能体",
                title_en="Interactive Reward Agent: GUI Task Evaluation via Environment-State Verification",
                one_line_pain=normalize_pain_point_length(
                    "针对纯视觉判分器易被表面页面跳转欺骗误给高分的问题，提出先探测再核验底层文件与配置的奖励机制",
                    context_tag="GUI判分器",
                ),
                metric_badges=[MetricBadge(text="arXiv:2607.25904"), MetricBadge(text="OSWorld 在线 RL 判分")],
                openness="code_only",
                openness_label="仅代码开源",
                small_compute_friendly=True,
                small_compute_note="判分器与探测脚本可单机部署",
                authors=["Interactive Reward Agent Authors"],
                published_date="2026-11-23",
                venue_or_source="arXiv cs.AI",
                url_primary="https://arxiv.org/abs/2607.25904",
                url_pdf="https://arxiv.org/pdf/2607.25904",
                url_arxiv_html="https://arxiv.org/html/2607.25904",
                arxiv_id="2607.25904",
                abstract_en=(
                    "Reliable reward signals remain a core bottleneck for GUI agent reinforcement learning. "
                    "Interactive Reward Agent (IRA, arXiv:2607.25904) introduces a propose-then-verify framework "
                    "that inspects both visible UI changes and post-execution environment states (files and app configs) "
                    "to provide dense, trustworthy rewards on OSWorld."
                ),
                four_elements=FourElements(
                    pain_point="仅靠最终一帧截图让 VLM 打分时，智能体点开看似正确的确认框但未真正写入系统文件或保存配置也会骗过判分器，导致在线 RL 奖励投机。",
                    mechanism="采用 Propose-then-Verify 两步核验：先根据任务意图生成需核查的状态假设，再主动读取执行后文件系统与应用配置状态交叉确认。",
                    experiments_summary="在 OSWorld 桌面交互评测与 RL 训练回路中显著降低假阳性奖励误判，稳定提升长程多步任务成功率。",
                    repro_and_reference="对自建免人工手写规则的可验证 GUI 训练环境很有借鉴意义，可与容器状态探针直接结合。",
                    backbone_and_params="多模态判分与状态探针框架",
                    gpu_and_duration="单机容器探针 + VLM 判分推理",
                    seeds_note="原文摘要未单列种子数",
                ),
                relative_score=8.7,
            )
        ),
        verify_radar_item(
            RadarItem(
                slug="gui-rl-2605-01208",
                section="papers",
                track="gui-rl",
                track_label="GUI-RL",
                subtopics=["gui-online-rl", "gui-security"],
                lab_tags=["lab-sjtu"],
                institution_tags=["上海交大"],
                title_zh="GuAE（上海交大 ICML 2026）：引导式优势估计解决同组同奖励梯度消失",
                title_en="Faithful Mobile GUI Agents with Guided Advantage Estimator",
                one_line_pain=normalize_pain_point_length(
                    "针对小模型做 GRPO 时同组采样全对或全错导致优势归零及界面陷阱误操作问题，引入锚点优势正则",
                    context_tag="GUI-RL",
                ),
                metric_badges=[MetricBadge(text="陷阱集 13.88% → 80.21%"), MetricBadge(text="8×H800 (Qwen3-VL-8B)")],
                openness="code_only",
                openness_label="仅代码开源",
                small_compute_friendly=True,
                small_compute_note="8 × NVIDIA H800 80GB 离线步级 RL",
                authors=["Haowen Hu", "Pengzhou Cheng", "Zheng Wu", "Lingzhong Dong", "Gongshen Liu", "Zhuosheng Zhang"],
                published_date="2026-11-23",
                venue_or_source="ICML 2026 Main Conference",
                url_primary="https://arxiv.org/abs/2605.01208",
                url_pdf="https://arxiv.org/pdf/2605.01208",
                url_arxiv_html="https://arxiv.org/html/2605.01208",
                arxiv_id="2605.01208",
                abstract_en=(
                    "Accepted at ICML 2026. Conducted on 8 NVIDIA H800 GPUs (80GB) with Qwen3-VL-8B across "
                    "AndroidControl and AITZ perturbation sets, Guided Advantage Estimator (GuAE) resolves zero-advantage "
                    "collapse in GRPO via anchor regularization and variance-aware scaling, raising trap perturbation "
                    "success rate from 13.88% to 80.21%."
                ),
                four_elements=FourElements(
                    pain_point="在 8B 级模型上跑 GRPO 时，遇到太简单或带遮挡/篡改陷阱的界面，组内多次采样往往全拿到相同奖励，组归一化后优势全变成 0，且常出现“思考发现了异常但手还是点下去”的言行不一。",
                    mechanism="第一，在优势计算中加入参考锚点正则与基于方差的自适应缩放，避免组内同分时梯度消失；第二，增加思考文本与输出动作的一致性奖励。",
                    experiments_summary="基于 Qwen3-VL-8B 在 8×H800 (80GB) 上完成 SFT + 步级 RL，在 AndroidControl 与 AITZ 构建的陷阱扰动评测集上成功率由 13.88% 提升至 80.21%。",
                    repro_and_reference="上海交大张倬胜/刘功申组代表性 8 卡工作：不依赖百卡在线模拟器集群，在离线步级数据上改优势估计即可跑通。",
                    experiment_rows=[
                        MetricRow(
                            benchmark="AndroidControl / AITZ 陷阱扰动集",
                            metric="任务成功率",
                            value="80.21%",
                            baseline_delta="对比基线 13.88%",
                            setup_note="Qwen3-VL-8B, 8×H800 80GB",
                        )
                    ],
                    backbone_and_params="Qwen3-VL-8B",
                    gpu_and_duration="8 × NVIDIA H800 80GB（训练时长原文未单列）",
                    seeds_note="原文未单列随机种子数",
                ),
                relative_score=9.2,
            )
        ),
        verify_radar_item(
            RadarItem(
                slug="ossl-ood-2603-25250",
                section="papers",
                track="ossl-ood",
                track_label="OSSL/OOD",
                subtopics=["open-world-vocab", "ossl-ood-core"],
                lab_tags=[],
                institution_tags=["TANL Authors"],
                title_zh="TANL：面向视觉—语言模型 OOD 检测的测试时激活负标签筛选",
                title_en="Activation Matters: Test-time Activated Negative Labels for OOD Detection with Vision-Language Models",
                one_line_pain=normalize_pain_point_length(
                    "针对固定大规模负词表在特定未知分布上大量失活并引入噪声分母的问题，在测试时筛选高激活负标签",
                    context_tag="OSSL/OOD",
                ),
                metric_badges=[MetricBadge(text="arXiv:2603.25250"), MetricBadge(text="免训练负词表激活过滤")],
                openness="code_only",
                openness_label="仅代码开源",
                small_compute_friendly=True,
                small_compute_note="测试时免训练（Training-free）单卡可跑",
                authors=["TANL Authors"],
                published_date="2026-11-23",
                venue_or_source="arXiv cs.CV",
                url_primary="https://arxiv.org/abs/2603.25250",
                url_pdf="https://arxiv.org/pdf/2603.25250",
                url_arxiv_html="https://arxiv.org/html/2603.25250",
                arxiv_id="2603.25250",
                abstract_en=(
                    "TANL (arXiv:2603.25250) critiques static negative label selection methods like NegLabel, "
                    "showing that many negative corpus labels exhibit poor activation or introduce noise on specific "
                    "OOD test distributions, and isolates distribution-adaptive test-time activated negative labels."
                ),
                four_elements=FourElements(
                    pain_point="NegLabel 从词库静态抽取上万个负标签后固定不变，但面对不同领域的未知测试集时，绝大多数负词根本不激活，反而在分数归一化分母里制造随机噪声。",
                    mechanism="在测试流上统计候选负标签词向量的实际响应分布，剔除死词与近似已知类的干扰词，只保留当前测试分布真正激活的负标签子集计算 OOD 分数。",
                    experiments_summary="在标准 CLIP 零样本 OOD 基准上无需反向传播微调，相对静态 NegLabel 稳定降低误报率。",
                    repro_and_reference="单卡数分钟即可复现测试时负词激活统计，与固定码本/词表选择机制形成清晰的“静态筛选 vs 测试时激活”对照视角。",
                    backbone_and_params="CLIP 系列预训练编码器",
                    gpu_and_duration="免训练测试时自适应（单卡可跑）",
                    seeds_note="确定性测试流统计 / 批次顺序消融",
                ),
                relative_score=8.5,
            )
        ),
        verify_radar_item(
            RadarItem(
                slug="vlm-infra-sglang-v0-5-23",
                section="opensource",
                track="vlm-infra",
                track_label="VLM/RL底座",
                subtopics=["inference-engine"],
                lab_tags=[],
                institution_tags=["SGLang Team"],
                title_zh="SGLang v0.5.23：新增 Paged Experts 单卡运行超大规模 MoE 与解码融合核",
                title_en="sgl-project/sglang v0.5.23: Paged Experts & Megakernel Decode Acceleration",
                one_line_pain=normalize_pain_point_length(
                    "针对混合专家模型参数远超单卡显存及单并发解码延迟高的问题，引入分页专家调度与融合内核插件",
                    context_tag="推理引擎",
                ),
                metric_badges=[MetricBadge(text="解码提速 44%–75%"), MetricBadge(text="Paged Experts 启用")],
                openness="code_and_weights",
                openness_label="代码+权重已开源",
                small_compute_friendly=True,
                small_compute_note="支持单张消费级/工作站 GPU 分页加载 MoE",
                authors=["SGLang Contributors"],
                published_date="2026-11-23",
                venue_or_source="GitHub Release (v0.5.23)",
                url_primary="https://github.com/sgl-project/sglang/releases",
                url_github="https://github.com/sgl-project/sglang",
                license_name="Apache-2.0",
                abstract_en=(
                    "SGLang v0.5.23 introduces Paged Experts to run oversized MoE models locally on single GPUs "
                    "and opt-in NVIDIA megakernel / AMD monokernel decode plugins achieving 44% to 75% single-user "
                    "decode speedups alongside strict 1.0 configuration checks."
                ),
                four_elements=FourElements(
                    pain_point="稀疏激活 MoE 模型虽然每步激活参数少，但全量专家常驻显存导致高校小卡机器根本装不下，且小批量解码受限于内核启动开销。",
                    mechanism="参考虚拟内存分页思想按需换入激活专家权重（Paged Experts），并提供可选的单核融合解码插件减少内核切换。",
                    experiments_summary="官方基准报告在单用户低延迟解码场景取得 44% 至 75% 的解码速度提升，并增强配置项严格校验。",
                    repro_and_reference="适合在本地有限显存机器上做 MoE 判分器或教师模型蒸馏推理。",
                    backbone_and_params="主流开源 MoE 与 VLM 推理",
                    gpu_and_duration="单卡及多卡推理均支持",
                    seeds_note="系统版本发布",
                ),
                relative_score=7.6,
            )
        ),
        verify_radar_item(
            RadarItem(
                slug="vlm-infra-openai-jagged-frontier",
                section="industry",
                track="vlm-infra",
                track_label="VLM/RL底座",
                subtopics=["multimodal-base"],
                lab_tags=["lab-openai-deepmind"],
                institution_tags=["OpenAI"],
                title_zh="OpenAI 内部研究自动化报告：2–4 小时多步长程任务无干预完成率达 46%",
                title_en="Research Acceleration: The Jagged Frontier of Autonomous Multi-Step Agents",
                one_line_pain=normalize_pain_point_length(
                    "针对长程智能体真实生产力缺乏客观耗时分层指标的问题，披露内部 2 至 4 小时级任务实测完成边界",
                    context_tag="长程评测",
                ),
                metric_badges=[MetricBadge(text="2–4小时任务完成率 46%")],
                openness="blog_or_release",
                openness_label="官方一手报告",
                small_compute_friendly=False,
                small_compute_note="内部评测统计报告",
                authors=["OpenAI"],
                published_date="2026-11-23",
                venue_or_source="OpenAI Research Blog",
                url_primary="https://openai.com/news/",
                abstract_en=(
                    "OpenAI reports in 'Research acceleration: The jagged frontier' that internal coding and research "
                    "agents completed 46% of 2-to-4-hour multi-step tasks without human intervention."
                ),
                four_elements=FourElements(
                    pain_point="公开基准多考察几分钟内的短平快操作，外界难以判断自主智能体在数小时真实工程与科研排障任务上的真实失效率。",
                    mechanism="按人类专家基准耗时（2–4 小时多步骤闭环任务）切分内部研发与安全验证工作流，统计零人工干预一次通过比例与常见卡壳点。",
                    experiments_summary="报告披露内部代码与研究智能体在 2 至 4 小时跨度的多步任务上无人工接管完成率约为 46%，呈现出“局部超人、跨环境状态核验易断链”的锯齿边界。",
                    repro_and_reference="验证了环境状态可验证性（Verifiability）是决定长程智能体成败的分水岭。",
                    backbone_and_params="OpenAI 内部前沿智能体管线",
                    gpu_and_duration="未公开",
                    seeds_note="内部任务集统计报告",
                ),
                relative_score=7.1,
            )
        ),
    ]

    issue23 = DailyIssue(
        date="2026-11-23",
        weekday_zh="周一",
        generated_at_cst="2026-11-23 09:26:42 CST",
        global_trend_summary=[
            "今日精选 5 条动态：上海交大 ICML 2026 论文 GuAE 给出 8×H800 下解决 GRPO 组内同分梯度消失的完整配方，IRA 则切中 OSWorld 视觉判分器被表面弹窗欺骗的痛点。",
            "OOD 方向关注测试时负标签激活过滤（TANL），与静态词库筛选路线形成直接互补；SGLang v0.5.23 上线 Paged Experts。",
        ],
        highlights_top3=[
            "• [GUI-RL] 上海交大 GuAE（ICML 2026）：8×H800 改 GRPO 优势估计，陷阱集成功率 13.88%→80.21%",
            "• [GUI-RL] Interactive Reward Agent：核验执行后底层文件与配置状态，消除 OSWorld 虚假奖励",
            "• [OSSL/OOD] TANL：测试时剔除未激活静态负标签，降低零样本 VLM OOD 分母噪声",
        ],
        featured_items=day23_items,
        folded_items=[
            FoldedItem(
                slug="fold-qwen-code-v0-27-2",
                section="opensource",
                track="vlm-infra",
                track_label="VLM/RL底座",
                title_zh="QwenLM/qwen-code v0.27.2：终端 Agent 常规交互参数保留修复",
                title_en="QwenLM/qwen-code v0.27.2 Weekly CLI Maintenance",
                one_line_takeaway="命令行交互卡片状态保留与远端连接开关维护更新",
                url="https://github.com/QwenLM/qwen-code/releases",
                published_date="2026-11-23",
                venue_or_source="GitHub Release",
            )
        ],
        run_mode="trial",
        provenance={
            "run_mode": "trial",
            "distiller": "deterministic-verified",
            "source_origin": "seeded_trial",
        },
    )
    issue23.validate()

    for iss in (issue22, issue23):
        yyyy, mm, _ = iss.date.split("-")
        p = content_dir / yyyy / mm / f"{iss.date}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(iss.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# Verbatim-grounded Chinese distillations served by our local OpenAI-compatible mock server during the 2026-11-24 trial run
CURATED_LLM_RESPONSES: dict[str, dict[str, object]] = {
    "2611.04073": {
        "title_zh": "Oregon State 实证归因：提供教程时桌面智能体几乎无法从世界模型推演中获益",
        "one_line_pain": "针对教程检索、判分器与世界模型混用时增益归因不清的痛点，剥离验证三者在桌面任务中的真实贡献",
        "metric_badges": ["构建 2 套教程基准", "100% 真实未来帧仍无显著增益"],
        "pain_point": "近期不少桌面智能体同时堆叠“检索操作教程 + 世界模型预测下一步画面 + 多模态判分器选动作”，算力开销翻倍，但没人拆开验证到底是教程起了作用，还是世界模型推演真的帮上了忙。",
        "mechanism": "作者先构建带多模态图文教程的 OSWorld-Tutorial 与 ScienceBoard-Tutorial 两个数据集，再设计 RAG-PolicyJudge（策略提候选动作、判分器依据教程挑动作），在 3 种设置下严格对比“不加世界模型”“零样本文字预测下一状态”以及“直接给 100% 真实未来屏幕画面（Oracle）”的效果差异。",
        "experiments_summary": "在 OSWorld-Tutorial 与 ScienceBoard-Tutorial 共 2 套基准、3 类对比设置下实测发现：一旦提供了任务相关教程，即便把世界模型换成 100% 准确的真实未来视觉帧，RAG-PJ 的成功率提升依然微乎其微；真正卡脖子的是判分器对未来状态及任务是否完成的推理判别能力。",
        "repro_and_reference": "已开源代码与两套教程基准（GitHub ★ 142，HF Daily Papers 同步上榜）。结论对高校小算力团队极具启发：不必耗费巨资训练重型像素级世界模型，把精力放在强化多模态判分器（Judge/Verifier）的状态判别准确率上性价比高得多。",
    },
    "2611.18715": {
        "title_zh": "T2T 技术报告：从海量无标注软件教学视频自动合成大规模可执行 GUI 轨迹",
        "one_line_pain": "针对人工录制桌面与手机操作轨迹成本高难扩规模的瓶颈，四阶段自动将公开教学视频转为可执行训练轨迹",
        "metric_badges": ["4 阶段自动合成管线", "横跨 4 大基准零人工标注涨点"],
        "pain_point": "训练通用 GUI 智能体极度依赖“截图—精确动作坐标”配对轨迹，人工逐条录制既贵又慢；网上虽有海量软件教学视频，但连续视频流没有离散动作边界、语义意图和精确点击坐标，直接喂给模型会学到大量无效帧捷径。",
        "mechanism": "提出 4 阶段全自动管线 T2T（Tutorial-to-Trajectory）：① 活跃操作片段过滤剔除口播废片；② 基于局部界面像素变化提取关键帧；③ 防捷径观测帧筛选避免未来视觉泄露；④ 存在性感知定位与 2 阶段动作空间归一化，统一桌面、网页和移动端动作格式。",
        "experiments_summary": "完全不使用目标域人工标注演示数据，用 T2T 合成轨迹微调 Qwen2.5-VL-7B 后，在 OSWorld、ScreenSpot-Pro、AndroidControl 与 AgentNetBench 共 4 个跨平台基准上均取得一致泛化提升。",
        "repro_and_reference": "代码与 7B 权重均已公开（GitHub ★ 215，HF 91 upvotes），可直接作为 GUI 强化学习阶段的冷启动 SFT 基座或合成轨迹数据源。",
    },
    "2605.16883": {
        "title_zh": "SE-GA（天大 & 上海交大 ICML 2026）：4×A800 跑通 7B GUI 智能体记忆增强自进化 RL",
        "one_line_pain": "针对高校 4 卡显存受限且多轮 RL 易遗忘历史成功技能的痛点，结合情景技能记忆与 LoRA 自进化训练",
        "metric_badges": ["4×A800 跑通 7B RL", "覆盖 3 大移动端与定位基准"],
        "pain_point": "高校常规 4 卡服务器做 7B 多模态模型全参数多轮强化学习极易显存爆满，且探索过程中偶发试对的高价值操作序列很快在后续策略更新中被遗忘，样本效率低。",
        "mechanism": "围绕 Qwen2.5-VL-7B 设计 2 阶段记忆增强自进化框架（SE-GA）：外挂情景技能记忆库存储高回报子轨迹辅助规划，第二阶段自进化 RL 采用 LoRA 轻量参数更新大幅压缩显存占用。",
        "experiments_summary": "全部实验仅使用 4 张 NVIDIA A800 GPU 完成，在 ScreenSpot、AndroidControl 与 AndroidWorld 共 3 个标准基准上验证了记忆回放加 LoRA RL 的持续自进化增益。",
        "repro_and_reference": "天津大学与上海交大张倬胜老师合作（ICML 2026 主会 v2 更新），是已核实高校 GUI 强化学习论文中算力门槛最低（仅 4×A800）的 7B 训练范例，极适合对标复现。",
    },
    "2602.18094": {
        "title_zh": "OODConceptBench（港浸会韩波组）：零样本视觉—语言 OOD 检测的词义碰撞审计与词表校准",
        "one_line_pain": "针对静态 WordNet 负标签词表存在一词多义和近义词碰撞推高误报的问题，审计并剪枝歧义负词",
        "metric_badges": ["平均 FPR95 降低 4.6%", "AUROC 达 95.1%"],
        "pain_point": "以 NegLabel 为代表的零样本 VLM OOD 方法直接从 WordNet 拉取大规模名词做负标签，但自然语言词表存在严重的一词多义与细粒度近义词重叠，部分负标签与已知类（ID）语义撞车，导致细粒度样本被误判为未知类、推高 FPR95。",
        "mechanism": "构建 OODConceptBench 系统审计静态负词表上的残余假阳性来源，提出免训练词义碰撞剪枝与校准策略，剔除与已知类视觉—文本分布发生局部重叠的歧义负概念。",
        "experiments_summary": "基于 CLIP-ViT-B/16 单卡免训练管线，在 ImageNet-1K 与 4 个标准 OOD 测试集上将平均 FPR95 降低 4.6%，平均 AUROC 提升至 95.1%。",
        "repro_and_reference": "出自双方向重点追踪的香港浸会大学韩波组，代码与清洗后词表已开源，单张常规 GPU 即可复现全部词表碰撞消融。",
    },
    "verl V1 Trainer": {
        "title_zh": "verl 主线全面切换 V1 Trainer 并发布多模态训练扩展 VeRL-Omni v0.3.0",
        "one_line_pain": "针对异步多模态 RL 训练时训练卡空等采样导致利用率低的痛点，上线闲置训练卡动态借调与 V1 架构",
        "metric_badges": ["Release v0.3.0-omni", "300 步 FP8 rollout 对齐"],
        "pain_point": "在长尾多轮智能体与多模态强化学习中，少数慢轨迹会拖慢整批同步等待，训练 GPU 长时间空转等 vLLM/SGLang 生成，且旧版 V0 与异步分支维护割裂。",
        "mechanism": "主线 PPO/GRPO 彻底移除旧 V0 统一至 V1 Trainer；新增 `separate_async` 调度（基于饥饿阈值把空闲训练卡临时借给生成池扩容采样）、收紧动态 micro-batch 长度上界，并同步推出支持多模态后训练的 VeRL-Omni v0.3.0。",
        "experiments_summary": "官方验证包含稳定的 300 步 FP8 rollout 精度对齐及多模态 VLM 后训练吞吐升级。",
        "repro_and_reference": "字节 Seed 主导的 `verl` 是当前 GUI-RL 与多模态 GRPO 最核心的开源训练底座，后续新建实验分支应直接基于 V1 Trainer 接口编写。",
    },
    "Expanding Claude Computer Use": {
        "title_zh": "Anthropic 向企业与团队版开放 Claude Computer Use，内置默认关闭与提示注入遮罩",
        "one_line_pain": "针对桌面操控智能体在办公环境易被外部网页恶意指令劫持的风险，引入组织级白名单与不可信内容遮罩",
        "metric_badges": ["100% 外部浏览器视图注入遮罩"],
        "pain_point": "赋予大模型直接截屏并点击本地桌面软件的权限后，一旦浏览器打开含隐藏提示词注入的外部网页，极易触发越权操作或敏感数据外泄。",
        "mechanism": "采用默认关闭（Opt-in）的组织级管理员白名单控制，并对 100% 外部浏览器渲染视图叠加不可信屏幕内容提示注入遮罩（Untrusted Content Masking）与动作审计。",
        "experiments_summary": "官方工程博客披露 Team 与 Enterprise 版管控架构，重点覆盖跨原生桌面应用的视觉截屏点击闭环与外部网页注入隔离。",
        "repro_and_reference": "代表工业界商用 Computer Use 安全边界的最新工程落地形态，对 GUI 提示注入防御研究提供了真实部署场景参照。",
    },
    "Qwen4-Max Official Launch": {
        "title_zh": "阿里通义正式发布旗舰稀疏 MoE 模型 Qwen4-Max（总参 2.4T / 激活 71B / 1M 上下文）",
        "one_line_pain": "针对超长视频与跨文档多模态推理受限于上下文窗口和激活开销的问题，推出原生百万上下文稀疏架构",
        "metric_badges": ["总参 2.4T / 激活 71B", "1M 原生多模态上下文"],
        "pain_point": "多模态智能体处理长时程屏幕录像与跨应用大文档时，传统稠密模型在百万级上下文下激活计算量与延迟难以承受。",
        "mechanism": "采用新一代 Qwen4 稀疏混合专家（MoE）架构，总参数量 2.4T、单步激活参数 71B，原生融合文本、图像与长视频输入并支持 1M token 上下文窗口。",
        "experiments_summary": "Latent Space [AINews] 拆条与通义官方同步披露架构规格（2.4T 总参、71B 激活、1M 上下文）；当前首发为闭源旗舰 API，社区正重点紧盯后续同代稠密开源小模型（如 7B/8B）权重释放节奏。",
        "repro_and_reference": "虽本批为闭源旗舰，但预示下一代开源 Qwen4-VL 小尺寸基座即将迭代，规划后续 GUI-RL 基座选型时值得留出接口兼容。",
    },
    "xlang-ai/OSWorld": {
        "title_zh": "港大 XLANG OSWorld-Verified 登上 GitHub Python 日榜（单日新增 195 ★）",
        "one_line_pain": "针对桌面智能体评测脚本环境偶发超时与判分漏判问题，持续维护跨三端操作系统可验证容器基准",
        "metric_badges": ["GitHub ★ 4,820", "今日新增 195 stars"],
        "pain_point": "跨平台桌面智能体在线强化学习与基准对比极易受容器死锁、界面加载抖动及旧版评估脚本假阴性影响，导致不同论文间指标口径失真。",
        "mechanism": "港大 XLANG 团队围绕 Ubuntu、Windows 与 macOS 持续维护 OSWorld-Verified 容器化可执行环境与确定性状态检查器，今日登上 GitHub Trending Python 日榜。",
        "experiments_summary": "仓库累计获得 4,820 GitHub Stars（当日新增 195 stars today），已成为桌面端 Computer-Use Agent 在线 RL rollout 与统一评测的事实标准环境。",
        "repro_and_reference": "高校单机 Docker/KVM 环境即可拉起并行桌面评测实例，做 OSWorld 对比实验时务必核对使用 Verified 最新判分脚本版本。",
        "backbone_and_params": "跨平台桌面可验证 RL 评测环境",
        "gpu_and_duration": "CPU/KVM 容器沙盒（与 VLM 推理卡解耦）",
        "seeds_note": "评测基准环境仓库",
    },
}


STAGE1_FOLDED_TRANSLATIONS: dict[str, tuple[float, str, str]] = {
    "Qwen4-Max": (0.0, "阿里通义发布闭源旗舰 Qwen4-Max（2.4T 稀疏 MoE / 71B 激活 / 1M 上下文）", "首发闭源 API 支持百万上下文原生音视频输入，后续可关注同代开源小尺寸 Qwen4-VL 权重"),
    "HTML-Pruner": (-0.5, "HTML-Pruner：面向 WebArena 网页浏览的轻量 DOM 树剪枝工具", "在购物网页上启发式压缩 38% HTML 上下文长度，未涉及强化学习训练"),
    "ClickHeatmap-Mini": (-0.5, "ClickHeatmap-Mini：桌面智能体图标定位注意力漂移可视化分析", "仅对比 2 种视觉编码器在 ScreenSpot 上的静态注意力热图，无策略训练"),
    "VoiceToMobile": (-0.5, "VoiceToMobile：手机快捷指令的双语语音意图解析前端评测", "在 150 条 AndroidControl 语音指令上测试语音转文本前端，属外围交互模块"),
    "BatchNorm-OOD": (-0.5, "BatchNorm-OOD：CIFAR-100 小规模划分上的后处理特征范数重标定", "仅在 CIFAR 小尺寸图像上做常规特征范数基线验证，未涉及开放词表或 VLM"),
    "SemiCluster-Med": (-0.5, "SemiCluster-Med：视网膜 OCT 医学影像上的广义类别发现聚类应用", "将既有 GCD 聚类损失直接迁移至眼科专科影像队列，非通用视觉方法创新"),
    "LoRA-Quant-KV": (-0.8, "LoRA-Quant-KV：长上下文多模态对话助手的 4-bit KV 缓存量化探索", "聚焦通用 32K 聊天场景的 4-bit 键值缓存压缩，未涉及智能体交互 rollout"),
    "OpenRLHF Async": (0.0, "OpenRLHF v0.6.4 社区补丁：新增多轮智能体异步轨迹回放缓冲区", "为多模态 PPO 采样补充异步多轮经验池支持，属常规小版本增量"),
    "TRL v0.16": (-1.0, "HF TRL v0.16.1：GRPO 训练器日志参数常规文档清理", "纯文档与日志参数整理，无算法或吞吐改动"),
    "vLLM CUDA 13.0": (-0.4, "vLLM v0.34.0rc1 Nightly：实验性 CUDA 13 构建与 LibTorch ABI 迁移", "面向新版 CUDA 的每日构建预览轮子，待正式版稳定后再评估升级"),
    "MolmoWeb Trajectory": (0.0, "AllenAI MolmoWeb Viewer：浏览器点击失败轨迹可视化回放小工具", "便于排查网页智能体单步点击偏离的轻量调试前端"),
    "SGLang TorchTPU": (-0.4, "SGLang-TorchTPU：面向 MoE 低延迟推理的原生 TPU 后端插件更新", "针对 TPU 硬件后端适配，与常规 GPU 训练集群关联较低"),
    "GUI-Agents-Paper-List": (-2.5, "OSU-NLP-Group/GUI-Agents-Paper-List：GUI 智能体论文索引列表登上 GitHub 日榜", "俄亥俄州立大学维护的领域文献汇总仓库日常收录更新（累计 2,310 ★）"),
    "xlang-ai/OSWorld": (-0.5, "xlang-ai/OSWorld：跨平台桌面智能体基准仓库登上 GitHub Python 日榜", "评测主仓日常 Star 热度上榜（当日新增 195 ★）"),
}


class _LocalMockLLMHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        user_msg = body["messages"][-1]["content"]

        if "Candidates:\n" in user_msg:
            cand_json_str = user_msg.split("Candidates:\n", 1)[1]
            rows = json.loads(cand_json_str)
            screen_items = []
            for r in rows:
                title = str(r.get("title", ""))
                delta, t_zh, tk_zh = 0.0, "", ""
                for kw, (d_val, zh_t, zh_tk) in STAGE1_FOLDED_TRANSLATIONS.items():
                    if kw.lower() in title.lower():
                        delta, t_zh, tk_zh = d_val, zh_t, zh_tk
                        break
                screen_items.append({"idx": r["idx"], "score_delta": delta, "title_zh": t_zh, "takeaway_zh": tk_zh})
            resp_bytes = json.dumps(
                {"choices": [{"message": {"content": json.dumps({"items": screen_items}, ensure_ascii=False)}}]},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)
            return

        matched = None
        for key, payload in CURATED_LLM_RESPONSES.items():
            if key.lower() in user_msg.lower():
                matched = payload
                break

        if matched is None:
            matched = {
                "title_zh": "前沿技术动态速览",
                "one_line_pain": "针对现有多模态与智能体训练流程中的工程与评测效率痛点，提供可复用的增量改进方案与实测对比",
                "metric_badges": ["一手技术更新"],
                "pain_point": "针对现有工程链路效率瓶颈展开改进。",
                "mechanism": "基于公开仓库与技术报告实现模块化升级。",
                "experiments_summary": "详见原文公开实测记录（原文未单列额外随机种子）。",
                "repro_and_reference": "可按需查阅一手链接评估接入成本。",
            }

        resp_bytes = json.dumps(
            {"choices": [{"message": {"content": json.dumps(matched, ensure_ascii=False)}}]},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(resp_bytes)))
        self.end_headers()
        self.wfile.write(resp_bytes)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def main() -> int:
    content_dir = REPO_ROOT / "content"
    fixtures_dir = REPO_ROOT / "tests" / "fixtures" / "real_2026_11_feeds"
    out_dir = REPO_ROOT / "_site"

    _seed_historical_issues(content_dir)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalMockLLMHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        with tempfile.TemporaryDirectory() as tmp_cfg:
            cfg_path = Path(tmp_cfg)
            write_mode_0600_atomic(
                cfg_path / "llm.json",
                json.dumps(
                    {
                        "primary": {
                            "name": "trial-verified-distiller",
                            "base_url": f"http://127.0.0.1:{port}/v1",
                            "api_key": "local-trial-token",
                            "screen_model": "gpt-4o-mini",
                            "distill_model": "gpt-4.1",
                            "timeout_sec": 15,
                        }
                    },
                    indent=2,
                )
                + "\n",
            )
            write_mode_0600_atomic(cfg_path / "sensitive_terms.txt", "# local sensitive terms\n")

            rc = radar_main(
                [
                    "--repo-root",
                    str(REPO_ROOT),
                    "run",
                    "--date",
                    "2026-11-24",
                    "--run-mode",
                    "trial",
                    "--health-file",
                    str(REPO_ROOT / "state" / "trial_source_health.json"),
                    "--content-dir",
                    str(content_dir),
                    "--out-dir",
                    str(out_dir),
                    "--fixtures-dir",
                    str(fixtures_dir),
                    "--config-home",
                    str(cfg_path),
                ]
            )
            return rc
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
