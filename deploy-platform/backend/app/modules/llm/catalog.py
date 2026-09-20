"""内置厂商与常用模型（参考 AIpeilian init.sql，仅预置目录；密钥需管理员填写）。"""
from __future__ import annotations

# code, name, website, description, api_base_url, sort_order
BUILTIN_PROVIDERS: list[tuple[str, str, str, str, str, int]] = [
    ("openai", "OpenAI", "https://openai.com", "GPT 系列", "https://api.openai.com/v1", 1),
    ("google", "Google", "https://deepmind.google", "Gemini OpenAI 兼容层", "https://generativelanguage.googleapis.com/v1beta/openai", 2),
    ("bytedance", "字节跳动", "https://www.volcengine.com", "豆包 / 火山方舟 OpenAI 兼容", "https://ark.cn-beijing.volces.com/api/v3", 11),
    ("alibaba", "阿里云", "https://www.aliyun.com", "通义千问·百炼 OpenAI 兼容", "https://dashscope.aliyuncs.com/compatible-mode/v1", 12),
    ("zhipu", "智谱AI", "https://www.zhipuai.cn", "GLM OpenAI 兼容", "https://open.bigmodel.cn/api/paas/v4", 13),
    ("deepseek", "DeepSeek", "https://www.deepseek.com", "DeepSeek 官方 OpenAI 兼容", "https://api.deepseek.com", 14),
    ("moonshot", "月之暗面", "https://www.moonshot.cn", "Kimi OpenAI 兼容", "https://api.moonshot.cn/v1", 15),
    ("baidu", "百度", "https://cloud.baidu.com", "文心·千帆 OpenAI 兼容", "https://qianfan.baidubce.com/v2", 16),
    ("tencent", "腾讯混元", "https://cloud.tencent.com", "混元 OpenAI 兼容（与 DeepSeek 不是同一套 Key）", "https://api.hunyuan.cloud.tencent.com/v1", 18),
    (
        "tencent-lkeap",
        "腾讯云 DeepSeek",
        "https://cloud.tencent.com/document/product/1823",
        "TokenHub OpenAI 兼容。控制台 API Key 管理创建密钥。Token Plan 用户把地址改成 https://api.lkeap.cloud.tencent.com/plan/v3",
        "https://tokenhub.tencentmaas.com/v1",
        17,
    ),
]

# provider_code, name, model_id, description, max_tokens, is_default, sort_order
BUILTIN_MODELS: list[tuple[str, str, str, str, int, bool, int]] = [
    ("openai", "GPT-4o mini", "gpt-4o-mini", "便宜、速度快", 4096, False, 1),
    ("openai", "GPT-4o", "gpt-4o", "OpenAI 旗舰多模态", 8192, False, 2),
    ("alibaba", "通义千问 Plus", "qwen-plus", "百炼 Plus，中文运维场景稳", 8192, False, 10),
    ("alibaba", "通义千问 Flash", "qwen-flash", "轻量快速", 4096, False, 11),
    ("alibaba", "通义千问 Max", "qwen-max", "旗舰推理", 8192, False, 12),
    ("deepseek", "DeepSeek Chat", "deepseek-chat", "DeepSeek 官方对话", 8192, False, 20),
    ("deepseek", "DeepSeek Reasoner", "deepseek-reasoner", "DeepSeek 官方推理", 8192, False, 21),
    ("tencent-lkeap", "DeepSeek V4 Pro", "deepseek-v4-pro", "腾讯云托管 DeepSeek，诊断默认模型", 8192, True, 15),
    ("tencent-lkeap", "DeepSeek V4 Flash", "deepseek-v4-flash", "腾讯云 DeepSeek V4，更快更省", 8192, False, 16),
    ("bytedance", "豆包 Pro", "Doubao-pro-32k", "火山方舟需填接入点 ID 为 model_id", 8192, False, 30),
    ("zhipu", "GLM-4", "glm-4", "智谱标准版", 4096, False, 40),
    ("moonshot", "Kimi", "moonshot-v1-32k", "长上下文", 8192, False, 50),
    ("google", "Gemini 2.0 Flash", "gemini-2.0-flash", "Google AI Studio 兼容层", 8192, False, 60),
    ("baidu", "文心 4.0", "ernie-4.0-8k", "千帆 ModelBuilder", 4096, False, 70),
    ("tencent", "混元", "hunyuan-turbo", "腾讯混元", 4096, False, 80),
]
