"""
JD 分析模块
通过 DeepSeek AI 分析岗位描述，判断与求职者的匹配度
"""

import json
import time
import re
from datetime import datetime

from utils import setup_logger

logger = setup_logger("analyzer")

# ── 系统提示词模板 ──

SYSTEM_PROMPT_TEMPLATE = """你是一位专业的招聘匹配分析助手。你的任务是根据求职者的背景信息与岗位 JD，判断是否应该投递该岗位。

## 规则

1. 诚实评估，不要为了"鼓励"而提高分数
2. 如果岗位要求明显超出求职者能力范围，应建议跳过
3. 如果岗位要求与求职者背景基本匹配或低于求职者能力，应建议投递
4. 薪资过低（远低于求职者期望）时应建议跳过
5. 请严格按照 JSON 格式输出，不要包含其他内容
6. 先判断岗位的主要日常职责和工作性质，再判断技能关键词；若主要工作并非求职目标方向，即使 JD 中有 Linux、Docker 等词，也应建议跳过
7. 对工作年限要求要与实际经历核对；项目实践可以提供匹配证据，但不能凭技能列表假定已有多年工作经验

## 求职者信息

{resume_text}

## 岗位 JD

{job_description}

## 评分标准（严格按以下维度加权评分）

{scoring_rules}

如果 match_score >= {threshold} 分，则 should_apply 为 true

## 岗位质量判断（v3.1，用于低质岗位拉黑）

在输出 job_quality 时如实判断：
- is_outsourcing：岗位是否为劳务派遣/外包/驻场/外派等非正式雇佣形式
- title_jd_consistent：岗位名称与 JD 实际职责是否相符（名不副实则 false）
- jd_far_exceeds_resume：JD 要求是否远超求职者（方向对但资历够不着，未来可能合适）

## 输出格式（仅返回 JSON，不要其他内容）

```json
{{
  "match_score": 0-100,
  "should_apply": true/false,
  "reason": "一句话理由（10字以内）",
  "key_matches": ["匹配点1", "匹配点2"],
  "key_gaps": ["不足点1"],
  "job_quality": {{
    "is_outsourcing": false,
    "title_jd_consistent": true,
    "jd_far_exceeds_resume": false
  }}
}}
```

"""


class JDAnalyzer:
    """JD 分析器——使用 DeepSeek 评估岗位匹配度"""

    def __init__(self, api_key: str, api_base: str = "https://api.deepseek.com",
                 model: str = "deepseek-chat", http_proxy: str = ""):
        self.api_key = api_key
        self.api_base = api_base
        self.model = model
        self.http_proxy = http_proxy
        self.llm = None
        self._http_client = None
        self.last_request_ok = None
        self.last_request_at = ""
        self.last_error = ""
        self._init_llm()

    def _init_llm(self):
        """初始化 DeepSeek LLM"""
        if not self.api_key:
            logger.warning("⚠️ DeepSeek API Key 未配置，AI 分析功能不可用")
            return

        try:
            import httpx
            from langchain_openai import ChatOpenAI

            options = {}
            if self.http_proxy:
                self._http_client = httpx.Client(proxy=self.http_proxy, timeout=30)
                options["http_client"] = self._http_client
                logger.info("🌐 AI 已配置本机 HTTP 代理")

            self.llm = ChatOpenAI(
                model=self.model,
                api_key=self.api_key,
                base_url=self.api_base,
                temperature=0.1,
                max_tokens=2048,
                **options,
            )
            # 测试连通性
            logger.info("✅ DeepSeek AI 初始化成功")
        except ImportError as e:
            logger.error(f"❌ langchain-openai 导入失败: {e}")
            logger.error("   请确认 conda 环境已安装: pip install langchain-openai")
            self.llm = None
        except Exception as e:
            logger.error(f"❌ DeepSeek 初始化失败: {e}")
            self.llm = None
            self.close()

    def close(self):
        if self._http_client:
            self._http_client.close()
            self._http_client = None

    def connection_status(self):
        return {"ready": self.is_ready(), "proxy_configured": bool(self.http_proxy),
                "last_request_ok": self.last_request_ok,
                "last_request_at": self.last_request_at,
                "last_error": self.last_error}

    def is_ready(self) -> bool:
        """检查 AI 是否可用"""
        return self.llm is not None

    def analyze(self, jd_text: str, resume_text: str, threshold: int = 60,
                scoring_rules: str = "") -> dict:
        """分析岗位 JD，返回匹配度评估

        scoring_rules: 自定义评分维度规则文本（来自 config.json 的 scoring 节）
        """
        if not self.is_ready():
            return self._fallback_decision()

        # 构建完整的 prompt
        prompt = SYSTEM_PROMPT_TEMPLATE.format(
            resume_text=resume_text,
            job_description=jd_text,
            threshold=threshold,
            scoring_rules=scoring_rules or "无自定义评分维度，请根据岗位 JD 与求职者简历自行综合评估",
        )

        for attempt in range(2):
            try:
                logger.info("  🤖 AI 正在分析岗位匹配度...")
                response = self.llm.invoke(prompt)
                content = response.content if hasattr(response, "content") else str(response)
                if isinstance(content, list):
                    content = "\n".join(str(block.get("text", "")) if isinstance(block, dict)
                                        else str(block) for block in content)
                result = self._parse_json_response(str(content), threshold)
                self.last_request_ok = True
                self.last_request_at = datetime.now().isoformat(timespec="seconds")
                self.last_error = ""
                logger.info(f"  📊 匹配度: {result.get('match_score', '?')}分 → "
                            f"{'✅ 投递' if result.get('should_apply') else '⏭️ 跳过'}"
                            f" | {result.get('reason', '')}")
                return result
            except (json.JSONDecodeError, ValueError, TypeError):
                self.last_request_ok = False
                self.last_request_at = datetime.now().isoformat(timespec="seconds")
                self.last_error = "AI 返回格式异常"
                if attempt == 0:
                    logger.warning("  ⚠️ AI 返回格式异常，重试一次")
                    prompt += "\n请只输出一个完整 JSON 对象，勿输出解释或代码块。"
                else:
                    logger.warning("  ⚠️ AI 返回格式仍异常，跳过该岗位")
            except Exception as e:
                self.last_request_ok = False
                self.last_request_at = datetime.now().isoformat(timespec="seconds")
                self.last_error = f"{type(e).__name__}: {e}"[:160]
                logger.warning(f"  ⚠️ AI 分析失败: {e}")
                if attempt == 0:
                    logger.info("  🔄 3 秒后重试一次 AI 连接")
                    time.sleep(3)
                    continue
                return self._fallback_decision()
        return self._fallback_decision()

    def _parse_json_response(self, text: str, threshold: int = 60) -> dict:
        """从 AI 返回文本中提取 JSON"""
        # 尝试直接解析
        text = text.strip()

        # 去掉可能的 markdown 代码块标记
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

        # Providers may wrap a valid object in prose or return several blocks.
        decoder = json.JSONDecoder()
        result = None
        for match in re.finditer(r'\{', text):
            try:
                candidate, _ = decoder.raw_decode(text[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "match_score" in candidate:
                result = candidate
                break
        if result is None:
            raise json.JSONDecodeError("未找到完整岗位评分 JSON", text, 0)
        if not isinstance(result, dict):
            raise ValueError("AI 返回内容不是岗位评估对象")
        raw_score = result.get("match_score")
        if isinstance(raw_score, bool):
            raise ValueError("AI 匹配分数格式错误")
        score = int(raw_score)
        if not 0 <= score <= 100:
            raise ValueError("AI 匹配分数超出范围")

        # 确保必需字段存在
        job_quality = result.get("job_quality", {}) or {}
        if not isinstance(job_quality, dict):
            job_quality = {}
        return {
            "match_score": score,
            "should_apply": result.get("should_apply") is True and score >= threshold,
            "reason": result.get("reason", "AI 分析失败"),
            "key_matches": result.get("key_matches", []),
            "key_gaps": result.get("key_gaps", []),
            "job_quality": {
                "is_outsourcing": job_quality.get("is_outsourcing") is True,
                "title_jd_consistent": job_quality.get("title_jd_consistent", True) is not False,
                "jd_far_exceeds_resume": job_quality.get("jd_far_exceeds_resume") is True,
            },
        }

    def _fallback_decision(self) -> dict:
        """降级策略：AI 不可用时默认不投递（安全优先）"""
        return {
            "match_score": 0,
            "should_apply": False,
            "reason": "AI 不可用",
            "key_matches": [],
            "key_gaps": ["AI 分析未执行"],
            "job_quality": {
                "is_outsourcing": False,
                "title_jd_consistent": True,
                "jd_far_exceeds_resume": False,
            },
        }
