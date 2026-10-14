"""
配置管理模块
负责读取 config.json、resume.json、.env 等配置文件
"""

import json
import math
import os
from pathlib import Path
from dotenv import load_dotenv


# 项目根目录
ROOT_DIR = Path(__file__).parent


def load_config() -> dict:
    """读取 config.json"""
    config_path = ROOT_DIR / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {config_path}\n"
            f"   请先执行: copy config.example.json config.json（或 cp config.example.json config.json）"
        )
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_resume() -> dict:
    """读取 resume.json"""
    resume_path = ROOT_DIR / "resume.json"
    if not resume_path.exists():
        raise FileNotFoundError(
            f"简历文件不存在: {resume_path}\n"
            f"   请先执行: copy resume.example.json resume.json（或 cp resume.example.json resume.json）"
        )
    with open(resume_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_env() -> dict:
    """读取 .env 环境变量（DeepSeek API Key）"""
    env_path = ROOT_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    api_base = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
    model = os.getenv("AI_MODEL", "deepseek-chat")

    if not api_key or api_key == "sk-your-deepseek-api-key-here":
        print("⚠️  警告：未配置 DeepSeek API Key！")
        print("   请编辑 .env 文件，填入你的 DEEPSEEK_API_KEY")
        print("   如果没有 API Key，请访问 https://platform.deepseek.com/ 注册获取\n")

    return {
        "api_key": api_key,
        "api_base": api_base,
        "model": model,
    }


def build_resume_text(resume: dict) -> str:
    """将 resume.json 转为自然语言文本，供 AI 分析使用"""
    parts = []

    # 基本信息
    parts.append(f"姓名：{resume.get('name', '未知')}")
    parts.append(f"学历：{resume.get('education', '未知')} · {resume.get('school', '')} · {resume.get('major', '')}")
    parts.append(f"毕业时间：{resume.get('graduation_year', '未知')}届")

    # 求职意向
    exp = resume.get("expectations", {})
    target_roles = "、".join(resume.get("target_roles", []))
    parts.append(f"求职意向：{target_roles}（{exp.get('job_type', '')}）")

    # 薪资（支持对象格式：实习/正式分开）
    salary = exp.get('salary', '面议')
    if isinstance(salary, dict):
        salary_parts = []
        if salary.get('internship'):
            salary_parts.append(f"实习{salary['internship']}")
        if salary.get('full_time'):
            salary_parts.append(f"正式{salary['full_time']}")
        salary_str = " / ".join(salary_parts) if salary_parts else "面议"
    else:
        salary_str = str(salary)
    weekend_info = "（要求双休）" if exp.get('require_weekend') else ""
    parts.append(f"意向城市：{exp.get('city', '未指定')} · 期望薪资：{salary_str}{weekend_info}")

    # 技能
    skills = resume.get("skills", {})
    all_skills = []
    for category, skill_list in skills.items():
        all_skills.extend(skill_list if isinstance(skill_list, list) else [skill_list])
    parts.append(f"技能：{'、'.join(all_skills)}")

    # 实习经历
    for exp_item in resume.get("experience", []):
        parts.append(f"实习经历：{exp_item.get('company', '')} · {exp_item.get('role', '')}（{exp_item.get('period', '')}）")
        parts.append(f"  {exp_item.get('description', '')}")

    # 项目经历
    for proj in resume.get("projects", []):
        parts.append(f"项目经历：{proj.get('name', '')} · {proj.get('role', '')}（{proj.get('period', '')}）")
        parts.append(f"  {proj.get('description', '')}")

    # 突出成果
    highlights = resume.get("highlights", [])
    if highlights:
        parts.append(f"突出成果：{'；'.join(highlights)}")

    # ⭐ v3.2 用户上传简历附件（.docx / .pdf 提取的全文，合并补充，供 AI 综合评分）
    try:
        from resume_upload import get_uploaded_text
        uploaded = get_uploaded_text()
        if uploaded:
            parts.append("【用户上传简历原文】")
            parts.append(uploaded)
    except Exception:
        pass

    return "\n".join(parts)


def build_scoring_rules(config: dict) -> str:
    """将 config.json 中的自定义评分维度转为自然语言评分规则，供 AI 使用

    用户可在控制面板增删改评分维度（名称/权重/说明/是否启用），
    此函数把配置实时转换为 Prompt 中的「评分标准」段落。
    """
    scoring = config.get("scoring", {})
    dimensions = scoring.get("dimensions", [])
    enabled = [d for d in dimensions if d.get("enabled", True)]

    if not enabled:
        return "无自定义评分维度，请根据岗位 JD 与求职者简历自行综合评估"

    lines = []
    total = 0
    for i, d in enumerate(enabled, 1):
        name = d.get("name") or f"维度{i}"
        weight = float(d.get("weight", 0))
        desc = d.get("description", "").strip()
        total += weight
        lines.append(f"{i}. {name}（权重 {weight:.0f}%）：{desc if desc else '综合评估该维度'}")
    note = f"各维度权重合计 {total:.0f}%，请严格按此比例分配各维度得分，match_score 为加权总分。"
    return "\n".join(lines) + "\n" + note


def validate_config(config: dict):
    """校验配置完整性"""
    required_keys = ["search", "ai", "application", "browser"]
    for key in required_keys:
        if key not in config:
            raise ValueError(f"配置缺少必要字段: {key}")

    search = config["search"]
    if not search.get("keyword"):
        raise ValueError("搜索关键词不能为空！请设置 config.json 中的 search.keyword")
    floors = search.get("salary_floor_by_city", {})
    if not isinstance(floors, dict):
        raise ValueError("城市最低月薪必须是 {城市: K数值} 对象")
    for city, value in floors.items():
        if not isinstance(city, str) or not city.strip():
            raise ValueError("最低月薪的城市名称不能为空")
        if isinstance(value, bool):
            raise ValueError(f"{city} 的最低月薪必须是数字")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{city} 的最低月薪必须是数字") from None
        if not math.isfinite(number) or not 0 < number <= 100:
            raise ValueError(f"{city} 的最低月薪应在 0–100K 之间")
    excluded = search.get("excluded_title_keywords", [])
    if not isinstance(excluded, list) or any(
            not isinstance(word, str) or not word.strip() for word in excluded):
        raise ValueError("排除岗位标题关键词必须是非空文本列表")
